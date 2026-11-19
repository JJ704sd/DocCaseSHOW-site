"""Local, fixed-sample HTTP entry to the existing P01/P02/P03 pipeline."""

import argparse
import copy
import functools
import hashlib
import json
import re
import subprocess
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4
from zipfile import ZipFile

import p02_local_read as p02
from deepseek_extraction import load_deepseek_agent
from local_email_index import IndexedSourceError, LocalEmailIndex, read_indexed_email
from local_inline_company import project_inline_company, publish_inline_company
from local_web_agent_context import (
    context_gaps,
    natural_gap_material_request,
    restrict_material_response,
    workbench_material_evidence,
)
from local_web_pipeline import saved_markdown

from doccase_preprocessing.body_objects import body_objects
from doccase_preprocessing.derived_fields import density_kg_per_m3
from doccase_preprocessing.local_handoff import _read_content, run_local_handoff
from doccase_preprocessing.main_channel import (
    ATTACHMENT_WEIGHT_PROFILE,
    BODY_WEIGHT_FIELDS,
    INQUIRY_FIELDS,
    LOCATION_INQUIRY_PROFILE,
    ROW_WEIGHT_FIELDS,
    SHIPMENT_WEIGHT_FIELDS,
    SPLIT_INQUIRY_FIELDS,
    WEIGHT_INQUIRY_PROFILE,
    _blocks,
    read_result,
    read_validated_result,
    run_main_channel,
    scoped_material_evidence,
)
from doccase_preprocessing.models import Limits
from doccase_preprocessing.weight_table import table_weight_object

PROJECT = Path(__file__).resolve().parents[2]
FIELD_CONTRACT = (
    PROJECT / "structured-output/integration-v03/snapshots/structured-output/evidence/"
    "parse-output-0.1.0.schema.json"
)
PARTIAL_REQUEST = PROJECT / "architecture-integration/fixtures/partial-request.json"

# Bundled input is synthetic. Requests never supply filesystem paths.
SAMPLES = {"missing-synthetic": {"profile": "partial-freight"}}
CURRENT_BODY_PROFILES = {
    "local-current-body-totals",
    "local-current-body-inquiry",
    LOCATION_INQUIRY_PROFILE,
    WEIGHT_INQUIRY_PROFILE,
}
INDEXED_PROFILES = {
    *CURRENT_BODY_PROFILES,
    "indexed-pdf-document",
    "indexed-pdf-row",
    "indexed-pdf-document-cargo",
    "indexed-pdf-row-cargo",
    ATTACHMENT_WEIGHT_PROFILE,
}


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _request(value):
    if not isinstance(value, dict) or set(value) != {"email_id", "profile_id"}:
        raise ValueError("Expected only email_id and profile_id")
    email_id = value["email_id"]
    profile_id = value["profile_id"]
    if not isinstance(email_id, str) or email_id not in SAMPLES:
        raise ValueError("Unknown email_id")
    if profile_id != SAMPLES[email_id]["profile"]:
        raise ValueError("Unknown or incompatible profile_id")
    return email_id, profile_id


def _input(sample, archive_root):
    if "archive" not in sample:
        return PARTIAL_REQUEST.read_bytes(), "json"
    archive_root = Path(archive_root).resolve(strict=True)
    path = (archive_root / sample["archive"]).resolve(strict=True)
    if not path.is_relative_to(archive_root):
        raise ValueError("Registered archive is outside the archive root")
    with ZipFile(path) as archive:
        info = archive.getinfo(sample["member"])
        limit = Limits().max_input_bytes
        if info.file_size > limit:
            raise ValueError("Registered email exceeds the input limit")
        with archive.open(info) as source:
            raw = source.read(limit + 1)
        if len(raw) != info.file_size or _sha(raw) != sample["email_sha256"]:
            raise ValueError("Registered email bytes differ from the fixed sample")
    return raw, "eml"


def _target(sample, manifest):
    materials = manifest["materials"]
    if "target_sha256" not in sample:
        matches = [m for m in materials if m["kind"] == "body" and m["format"] == "text"]
    else:
        matches = [
            m
            for m in materials
            if m["kind"] == sample["target_kind"]
            and m["format"] == sample["target_format"]
            and m.get("content")
            and m["content"]["sha256"] == sample["target_sha256"]
        ]
    if len(matches) != 1 or matches[0]["state"] != "ready":
        raise ValueError("Registered target material is missing, ambiguous or not ready")
    return matches[0]


def _profile(profile_id, manifest, target):
    mid = target["material_id"]
    dependencies = [m["material_id"] + "/representation" for m in manifest["materials"]]
    if profile_id == "body-cargo":
        fields = [("ctns", "Cargo details:"), ("line_volume", "Cargo details:")]
        object_name, source_kind = "quoted-cargo-details", "quoted_body"
    elif profile_id == "pdf-packing":
        fields = [("ctns", "TOTAL PACKED"), ("line_gross_weight", None), ("line_volume", None)]
        object_name, source_kind = "packing-list-total", "attachment"
    elif profile_id in {"six-pdf-row", "six-pdf-row-22"}:
        fields = [
            ("ctns", None),
            ("line_net_weight", None),
            ("line_gross_weight", None),
            ("unit_net_weight", None),
            ("unit_gross_weight", None),
            ("line_volume", None),
        ]
        object_name = "packing-row-23" if profile_id == "six-pdf-row" else "packing-row-01-22"
        source_kind = "attachment"
    else:
        fields = [
            ("ctns", None),
            ("line_gross_weight", None),
            ("unit_gross_weight", None),
            ("line_volume", None),
        ]
        object_name, source_kind = "L1", "current_body"
    requirements = []
    for field_name, pattern in fields:
        requirement = {
            "requirement_id": "REQ-" + field_name.upper(),
            "field_name": field_name,
            "material_id": mid,
            "object_selector": mid + "/" + object_name,
            "source_kind": source_kind,
            "critical": profile_id in {"six-pdf-row", "six-pdf-row-22"}
            or field_name != "unit_gross_weight",
            "missing_policy": "allow"
            if profile_id not in {"six-pdf-row", "six-pdf-row-22"}
            and field_name == "unit_gross_weight"
            else "block",
            "allow_not_applicable": False,
            "scope_dependencies": [{"stage": "extraction", "refs": dependencies}],
        }
        if pattern:
            requirement["source_pattern"] = pattern
        if profile_id == "body-cargo":
            requirement["quoted_section_markers"] = [
                "De:",
                "Enviado el:",
                "miércoles, 28 de octubre de 2026 10:40",
            ]
        if profile_id == "partial-freight":
            requirement["object_anchor"] = "L1"
        if profile_id == "pdf-packing" and field_name == "line_volume":
            requirement["pdf_total_volume"] = True
        if profile_id in {"six-pdf-row", "six-pdf-row-22"}:
            requirement["pdf_row_anchor"] = "#23" if profile_id == "six-pdf-row" else "#01~#22"
        requirements.append(requirement)
    return {
        "id": profile_id,
        "version": "2" if profile_id in {"body-cargo", "pdf-packing"} else "1",
        "task_scope_ref": manifest["task_id"],
        "requirements": requirements,
    }


def current_body_profile(manifest, body, profile_id="local-current-body-totals"):
    dependencies = [item["material_id"] + "/representation" for item in manifest["materials"]]
    requirements = []
    fields = ("package_count", "line_gross_weight", "line_volume", "ctns")
    if profile_id == "local-current-body-inquiry":
        fields += ("package_type", *INQUIRY_FIELDS)
    elif profile_id == LOCATION_INQUIRY_PROFILE:
        fields += ("package_type", *SPLIT_INQUIRY_FIELDS)
    elif profile_id == WEIGHT_INQUIRY_PROFILE:
        fields = (
            tuple(
                name
                for name in (*fields, "package_type", *SPLIT_INQUIRY_FIELDS)
                if name not in {"line_gross_weight", "chargeable_weight"}
            )
            + BODY_WEIGHT_FIELDS
        )
    for field in fields:
        requirements.append(
            {
                "requirement_id": "REQ-" + field.upper(),
                "field_name": field,
                "material_id": body["material_id"],
                "object_selector": body["material_id"] + "/document",
                "source_kind": "current_body",
                "critical": True,
                "missing_policy": "block",
                "allow_not_applicable": False,
                "scope_dependencies": [{"stage": "extraction", "refs": dependencies}],
            }
        )
    return {
        "id": profile_id,
        "version": "1",
        "task_scope_ref": manifest["task_id"],
        "requirements": requirements,
    }


def attachment_weight_profile(manifest, target, selector):
    mid = target["material_id"]
    object_id = table_weight_object(mid, selector)
    fields = ROW_WEIGHT_FIELDS if "row" in selector else SHIPMENT_WEIGHT_FIELDS
    return {
        "id": ATTACHMENT_WEIGHT_PROFILE,
        "version": "1",
        "task_scope_ref": manifest["task_id"],
        "requirements": [
            {
                "requirement_id": "REQ-" + name.upper(),
                "field_name": name,
                "material_id": mid,
                "object_selector": object_id,
                "weight_table": dict(selector),
                "source_kind": "attachment",
                "critical": True,
                "missing_policy": "block",
                "allow_not_applicable": False,
                "scope_dependencies": [{"stage": "extraction", "refs": [mid + "/representation"]}],
            }
            for name in fields
        ],
    }


def _indexed_profile(profile_id, manifest, target, row_anchor=None):
    mid = target["material_id"]
    dependencies = [item["material_id"] + "/representation" for item in manifest["materials"]]
    requirements = []
    fields = [
        "ctns",
        "line_net_weight",
        "line_gross_weight",
        "unit_net_weight",
        "unit_gross_weight",
        "line_volume",
    ]
    if profile_id in {"indexed-pdf-document-cargo", "indexed-pdf-row-cargo"}:
        fields.extend(
            [
                "product_name",
                "package_type",
                "package_count",
                "raw_observations",
                "length_cm",
                "width_cm",
                "height_cm",
            ]
        )
    for field in fields:
        requirement = {
            "requirement_id": "REQ-" + field.upper(),
            "field_name": field,
            "material_id": mid,
            "object_selector": mid + ("/selected-row" if row_anchor else "/document"),
            "source_kind": "attachment",
            "critical": True,
            "missing_policy": "block",
            "allow_not_applicable": False,
            "scope_dependencies": [{"stage": "extraction", "refs": dependencies}],
        }
        if row_anchor:
            requirement["pdf_row_anchor"] = row_anchor
        elif field in {"line_gross_weight", "line_volume"}:
            requirement["pdf_split_total"] = True
        if profile_id in {"indexed-pdf-document", "indexed-pdf-document-cargo"} and field in {
            "ctns",
            "line_net_weight",
            "line_gross_weight",
            "line_volume",
        }:
            requirement["pdf_table_total"] = True
        if profile_id in {"indexed-pdf-document-cargo", "indexed-pdf-row-cargo"} and (
            field
            in {
                "product_name",
                "package_type",
                "package_count",
                "raw_observations",
                "length_cm",
                "width_cm",
                "height_cm",
            }
            or (field == "ctns" and not row_anchor)
        ):
            requirement["pdf_cargo"] = True
        requirements.append(requirement)
    return {
        "id": profile_id,
        "version": {"indexed-pdf-document": "3", "indexed-pdf-document-cargo": "2"}.get(
            profile_id, "1"
        ),
        "task_scope_ref": manifest["task_id"],
        "requirements": requirements,
    }


def _indexed_target(manifest, source):
    expected_kind = "inline" if source["mime_disposition"] == "inline" else "attachment"
    matches = [
        item
        for item in manifest["materials"]
        if item["kind"] == expected_kind
        and item["metadata"].get("mime_path") == source["mime_path"]
    ]
    if len(matches) != 1:
        raise IndexedSourceError("Indexed MIME attachment was not registered by preprocessing")
    target = matches[0]
    cid = (source["mime_content_id"] or "").strip("<> \t\r\n") or None
    if (
        target["metadata"].get("disposition") != source["mime_disposition"]
        or target["filename"] != source["mime_filename"]
        or target.get("cid") != cid
        or (
            expected_kind == "inline"
            and target["metadata"].get("mime_filename") != source["mime_filename"]
        )
    ):
        raise IndexedSourceError("Prepared MIME metadata differs from the indexed email")
    if target["state"] == "ready" and (
        target["format"] != "pdf"
        or not target.get("content")
        or target["content"]["sha256"] != source["attachment_sha256"]
    ):
        raise IndexedSourceError("Prepared PDF differs from the indexed MIME attachment")
    return target


def _email_metadata(manifest, manifest_ref):
    """Expose single-email headers with pointers to the immutable P01 manifest."""
    email = manifest["emails"][0]
    if "raw_headers" not in email:
        return None
    present = {name.lower() for name, _ in email["raw_headers"]}
    headers = {
        "subject": "subject",
        "from_addr": "from",
        "from_name": "from",
        "to_addrs": "to",
        "cc_addrs": "cc",
        "sent_at": "date",
    }
    return {
        "email_ref_id": email["email_ref_id"],
        "scope": "single_email_header",
        "fields": {
            name: {
                "value": email.get(name) if header in present else None,
                "status": "known" if header in present and email.get(name) else "uncertain",
                "source": {**manifest_ref, "json_pointer": "/emails/0/" + name},
            }
            for name, header in headers.items()
        },
    }


def _box_overlap(first, second, start, end):
    if not all(
        isinstance(box.get(key), (int, float)) for box in (first, second) for key in (start, end)
    ):
        return False
    return max(min(first[start], first[end]), min(second[start], second[end])) < min(
        max(first[start], first[end]), max(second[start], second[end])
    )


def _pdf_product_rows(artifacts):
    """Expose numbered product descriptions from a structured PDF table."""
    rows = []
    for artifact in artifacts:
        document = json.loads(Path(artifact["ref"]).read_bytes())
        parts = (
            [
                (f"/parts/{index}/document", part["document"])
                for index, part in enumerate(document["parts"])
            ]
            if document.get("kind") == "docling_pdf_read_bundle"
            else [("", document)]
        )
        for prefix, part in parts:
            for table_index, table in enumerate(part.get("tables", [])):
                cells = table.get("data", {}).get("table_cells", [])
                indexed = list(enumerate(cells))
                item_headers = [
                    (i, cell)
                    for i, cell in indexed
                    if re.fullmatch(
                        r"\s*(?:item(?:\s*(?:no\.?|number))?|序号)\s*", cell.get("text", ""), re.I
                    )
                ]
                name_headers = [
                    (i, cell)
                    for i, cell in indexed
                    if re.fullmatch(
                        r"\s*(?:description(?:\s+of\s+goods)?|commodity|品名|货物品名)\s*",
                        cell.get("text", ""),
                        re.I,
                    )
                ]
                if len(item_headers) != 1 or len(name_headers) != 1:
                    continue
                item_index, item_header = item_headers[0]
                name_index, name_header = name_headers[0]
                header_row = item_header.get("start_row_offset_idx")
                item_col = item_header.get("start_col_offset_idx")
                name_col = name_header.get("start_col_offset_idx")
                name_end = name_header.get("end_col_offset_idx")
                if (
                    not all(isinstance(x, int) for x in (header_row, item_col, name_col, name_end))
                    or header_row != name_header.get("start_row_offset_idx")
                    or item_header.get("end_col_offset_idx") != item_col + 1
                    or item_col >= name_col
                    or name_end <= name_col
                ):
                    continue
                pages = {
                    entry.get("page_no") for entry in table.get("prov", []) if entry.get("page_no")
                }
                if len(pages) != 1:
                    continue
                quantity_specs = {
                    "line_net_weight": (
                        "kg",
                        r"(?:n\s*[./]?\s*w\.?|net\s*weight)\s*\(?\s*kgs?\s*\)?",
                    ),
                    "line_gross_weight": (
                        "kg",
                        r"(?:g\s*[./]?\s*w\.?|gross\s*weight)\s*\(?\s*kgs?\s*\)?",
                    ),
                    "line_volume": (
                        "CBM",
                        r"(?:(?:size|volume)\s*\(\s*m[³3]\s*\)|(?:total\s+)?cbm)",
                    ),
                }
                for item_index_row, item in indexed:
                    row = item.get("start_row_offset_idx")
                    if (
                        not isinstance(row, int)
                        or row <= header_row
                        or item.get("end_row_offset_idx") != row + 1
                        or item.get("start_col_offset_idx") != item_col
                        or item.get("end_col_offset_idx") != item_col + 1
                        or not re.fullmatch(r"\d+", item.get("text", "").strip())
                    ):
                        continue
                    names = [
                        (i, cell)
                        for i, cell in indexed
                        if cell.get("start_row_offset_idx") == row
                        and cell.get("end_row_offset_idx") == row + 1
                        and cell.get("start_col_offset_idx") == name_col
                        and cell.get("end_col_offset_idx") == name_end
                        and cell.get("text", "").strip()
                    ]
                    if len(names) != 1:
                        continue
                    description_index, description = names[0]
                    base = f"{prefix}/tables/{table_index}/data/table_cells/"
                    product = {
                        "scope": "product_row",
                        "item_no": item["text"].strip(),
                        "product_name": description["text"].strip(),
                        "source": {
                            "artifact_id": artifact["artifact_id"],
                            "ref": artifact["ref"],
                            "sha256": artifact["artifact_hash"],
                            "page": next(iter(pages)),
                            "item_header_pointer": base + str(item_index) + "/text",
                            "description_header_pointer": base + str(name_index) + "/text",
                            "item_pointer": base + str(item_index_row) + "/text",
                            "description_pointer": base + str(description_index) + "/text",
                        },
                    }
                    for field_name, (unit, pattern) in quantity_specs.items():
                        product[field_name] = {"value": None, "unit": unit, "status": "uncertain"}
                        headers = [
                            (i, cell)
                            for i, cell in indexed
                            if cell.get("start_row_offset_idx") == header_row
                            and re.fullmatch(r"\s*" + pattern + r"\s*", cell.get("text", ""), re.I)
                        ]
                        if len(headers) != 1:
                            continue
                        header_index, header = headers[0]
                        head_box = header.get("bbox", {})
                        item_box = item.get("bbox", {})
                        values = [
                            (i, cell)
                            for i, cell in indexed
                            if cell.get("start_row_offset_idx") == row
                            and cell.get("end_row_offset_idx") == row + 1
                            and re.fullmatch(r"\d+(?:\.\d+)?", cell.get("text", "").strip())
                            and _box_overlap(head_box, cell.get("bbox", {}), "l", "r")
                            and _box_overlap(item_box, cell.get("bbox", {}), "t", "b")
                        ]
                        if len(values) == 1:
                            value_index, value_cell = values[0]
                            product[field_name] = {
                                "value": float(value_cell["text"]),
                                "unit": unit,
                                "status": "known",
                                "source": {
                                    "ref": artifact["ref"],
                                    "sha256": artifact["artifact_hash"],
                                    "page": next(iter(pages)),
                                    "header_pointer": base + str(header_index) + "/text",
                                    "value_pointer": base + str(value_index) + "/text",
                                },
                            }
                    product["density_kg_per_m3"] = density_kg_per_m3(
                        product["line_gross_weight"]["value"], product["line_volume"]["value"]
                    )
                    rows.append(product)
    return rows


def _pdf_document_observations(artifacts):
    labels = {
        "shipment terms": "shipment_terms",
        "shipping way": "shipping_way",
        "loading port": "loading_port",
        "discharge port": "discharge_port",
    }
    observations = []
    for artifact in artifacts:
        document = json.loads(Path(artifact["ref"]).read_bytes())
        parts = (
            [
                (f"/parts/{index}/document", part["document"])
                for index, part in enumerate(document["parts"])
            ]
            if document.get("kind") == "docling_pdf_read_bundle"
            else [("", document)]
        )
        for prefix, part in parts:
            for table_index, table in enumerate(part.get("tables", [])):
                indexed = list(enumerate(table.get("data", {}).get("table_cells", [])))
                pages = {
                    entry.get("page_no") for entry in table.get("prov", []) if entry.get("page_no")
                }
                if len(pages) != 1:
                    continue
                for label_index, label in indexed:
                    name = labels.get(label.get("text", "").strip().casefold())
                    row = label.get("start_row_offset_idx")
                    if not name or not isinstance(row, int):
                        continue
                    values = [
                        (i, cell)
                        for i, cell in indexed
                        if cell.get("start_row_offset_idx") == row + 1
                        and cell.get("start_col_offset_idx") == label.get("start_col_offset_idx")
                        and cell.get("end_col_offset_idx") == label.get("end_col_offset_idx")
                        and cell.get("text", "").strip()
                    ]
                    if len(values) != 1:
                        continue
                    value_index, value = values[0]
                    base = f"{prefix}/tables/{table_index}/data/table_cells/"
                    observations.append(
                        {
                            "field_name": name,
                            "value": value["text"].strip(),
                            "scope": "document",
                            "source": {
                                "ref": artifact["ref"],
                                "sha256": artifact["artifact_hash"],
                                "page": next(iter(pages)),
                                "label_pointer": base + str(label_index) + "/text",
                                "value_pointer": base + str(value_index) + "/text",
                            },
                        }
                    )
    return observations


def _pdf_dimension_observations(artifacts):
    pattern = re.compile(
        r"\s*dimension\s*(?:(?P<first>\d+)\s*[:：]|[:：]\s*(?P<second>\d+)\s*#?)\s*"
        r"(?P<dimensions>\d+(?:\.\d+)?\s*[*x×]\s*\d+(?:\.\d+)?\s*[*x×]\s*\d+(?:\.\d+)?)"
        r"\s*cm\s*(?:(?P<weight>\d+(?:\.\d+)?)\s*kgs?)?\s*",
        re.I,
    )
    observations = []
    for artifact in artifacts:
        document = json.loads(Path(artifact["ref"]).read_bytes())
        parts = (
            [
                (f"/parts/{index}/document", part["document"])
                for index, part in enumerate(document["parts"])
            ]
            if document.get("kind") == "docling_pdf_read_bundle"
            else [("", document)]
        )
        for prefix, part in parts:
            found = []
            for index, item in enumerate(part.get("texts", [])):
                match = pattern.fullmatch(item.get("text", ""))
                pages = {
                    entry.get("page_no") for entry in item.get("prov", []) if entry.get("page_no")
                }
                if not match or len(pages) != 1:
                    continue
                found.append(
                    {
                        "scope": "document_dimension_observation",
                        "number": match.group("first") or match.group("second"),
                        "dimensions": match.group("dimensions").strip(),
                        "dimension_unit": "cm",
                        "dimension_order": "unspecified",
                        "weight_kind": "unspecified",
                        "weight_kg": float(match.group("weight"))
                        if match.group("weight")
                        else None,
                        "source": {
                            "ref": artifact["ref"],
                            "sha256": artifact["artifact_hash"],
                            "page": next(iter(pages)),
                            "json_pointer": f"{prefix}/texts/{index}/text",
                        },
                    }
                )
            if found:
                observations.extend(found)
                break
    return observations


def _response(email_id, profile_id, run_id, result, snapshot, read, manifest):
    handoff = snapshot["material_handoff"]
    sources = {item["source_id"]: item for item in handoff["sources"]}
    bindings = {item["source_id"]: item for item in handoff["source_bindings"]}
    issues = {item["issue_id"]: item for item in handoff["issues"]}
    assessments = {
        candidate_id: assessment
        for assessment in handoff["requirement_assessments"]
        for candidate_id in assessment["candidate_ids"]
    }
    fields = []
    for candidate in handoff["candidates"]:
        field = candidate["field"]
        assessment = assessments[candidate["candidate_id"]]
        fields.append(
            {
                "field_name": candidate["field_name"],
                "object_id": candidate["object_id"],
                "value": field["value"],
                "unit": (
                    "carton"
                    if candidate["field_name"] == "ctns" and field["value"] is not None
                    else field.get("unit")
                ),
                "status": field["status"],
                "sources": [
                    {"source": sources[sid], "binding": bindings[sid]}
                    for sid in field["source_ids"]
                ],
                "issues": [issues[iid] for iid in assessment["issue_ids"]],
            }
        )
        if profile_id in {"indexed-pdf-document-cargo", "indexed-pdf-row-cargo"}:
            fields[-1]["raw_values"] = field["raw_values"]
        if profile_id in {WEIGHT_INQUIRY_PROFILE, ATTACHMENT_WEIGHT_PROFILE}:
            fields[-1].update(
                quantity_scope=candidate["quantity_scope"],
                quantity_kind=candidate["quantity_kind"],
                raw_values=field["raw_values"],
            )
    # Multiple READ passes can support the same assessed value. Return one
    # field while keeping every candidate's source evidence.
    combined = []
    for field in fields:
        match = next(
            (
                item
                for item in combined
                if all(
                    item[key] == field[key]
                    for key in ("field_name", "object_id", "value", "unit", "status")
                )
            ),
            None,
        )
        if match is None:
            combined.append(field)
            continue
        source_ids = {item["source"]["source_id"] for item in match["sources"]}
        match["sources"].extend(
            item for item in field["sources"] if item["source"]["source_id"] not in source_ids
        )
        issue_ids = {item["issue_id"] for item in match["issues"]}
        match["issues"].extend(
            item for item in field["issues"] if item["issue_id"] not in issue_ids
        )
        if "raw_values" in field:
            match["raw_values"].extend(
                item for item in field["raw_values"] if item not in match["raw_values"]
            )
    fields = combined
    records = {item["material_id"]: item for item in manifest["materials"]}
    inventory = {item["material_id"]: item for item in read["inventory"]}
    materials = []
    for item in snapshot["per_material_results"]:
        record = records[item["material_id"]]
        observed = inventory[item["material_id"]]
        materials.append(
            {
                "material_id": item["material_id"],
                "filename": record["filename"],
                "kind": record["kind"],
                "format": record["format"],
                "mime_path": observed["mime_path"],
                "mime_disposition": record["metadata"].get("disposition"),
                "cid": record.get("cid"),
                "preprocessing_state": observed["preprocessing_state"],
                "read_state": item["read_state"],
                "reason": item["reason"]
                or (
                    item["preprocessing_issues"][0]["code"]
                    if item["read_state"] == "blocked" and item["preprocessing_issues"]
                    else None
                ),
                "preprocessing_issues": item["preprocessing_issues"],
                "read_issues": item["read_issues"],
            }
        )
    response = {
        "api": "local-parse/0.1",
        "run_id": run_id,
        "email_id": email_id,
        "profile_id": profile_id,
        "status": handoff["handoff_state"],
        "result_id": handoff["result_id"],
        "result_revision": handoff["result_revision"],
        "fields": fields,
        "materials": materials,
        "issues": handoff["issues"],
        "sources": handoff["sources"],
        "source_bindings": handoff["source_bindings"],
        "artifacts": handoff["artifact_refs"],
        "coverage": handoff["coverage"],
        "result_ref": result["result_ref"],
        "read_ref": result["read_ref"],
        "limitations": snapshot["analysis"]["limitations"],
    }
    metadata = _email_metadata(manifest, handoff["input_manifest_ref"])
    if metadata is not None:
        response["email_metadata"] = metadata
    if profile_id == "indexed-pdf-document-cargo":
        response["cargo_rows"] = _pdf_product_rows(handoff["artifact_refs"])
        response["document_observations"] = _pdf_document_observations(handoff["artifact_refs"])
        response["dimension_observations"] = _pdf_dimension_observations(handoff["artifact_refs"])
    execution = next(
        (
            item
            for item in read["executions"]
            if item.get("result", {}).get("read_strategy") == "adaptive_pdf"
        ),
        None,
    )
    if execution:
        observed = execution["result"]
        response["pdf_read"] = {
            "conversion_status": observed["conversion_status"],
            "termination_reason": observed.get("termination_reason"),
            "document_pages": observed.get("document_pages", []),
            "unread_pages": observed.get("unread_pages", []),
            "passes": observed.get("passes", []),
            "resolved_table_cells": observed.get("resolved_table_cells", []),
            "elapsed_seconds": observed.get("elapsed_seconds"),
        }
    return response


def _material_gap_replay(
    rules,
    *,
    root,
    p01,
    prepared,
    manifest,
    target,
    profile,
    configurations,
    versions,
    contract,
    worker,
    material_agent,
    extraction_agent,
):
    """Run the material Agent only for evidenced gaps on the existing rules READ."""
    snapshot = read_validated_result(rules["result_ref"], content_root=p01)
    read = read_result(rules["read_ref"])
    fields = [
        {
            "name": item["field_name"],
            "status": item["field"]["status"],
            "value": item["field"]["value"],
        }
        for item in snapshot["material_handoff"]["candidates"]
    ]
    sidecar = saved_markdown(rules, root, target["material_id"])
    inventory = {item["material_id"]: item for item in read["inventory"]}
    executions = {
        item["execution_id"]: item for item in read["executions"] if item.get("execution_id")
    }
    execution = executions.get(inventory[target["material_id"]].get("execution_id"))
    multi_shipment = False
    if target["kind"] == "body" and execution and execution.get("ref"):
        original = _read_content(p01.resolve(), target["content"]).decode("utf-8-sig")
        document, _ = _blocks(execution)
        email_sha = next(
            (
                item["original_sha256"]
                for item in read["inventory"]
                if item["kind"] == "original_email"
                and item["email_ref_id"] == target["email_ref_id"]
            ),
            target["content"]["sha256"],
        )
        objects = body_objects(
            document, original, email_sha=email_sha, material_sha=target["content"]["sha256"]
        )
        multi_shipment = (
            sum(item.get("source_role") == "current_body" for item in objects["shipments"]) > 1
        )

    evidence = functools.partial(workbench_material_evidence, context_gaps(fields))
    audit = {
        "state": "not_sent",
        "reason": "no_evidenced_gap",
        "requested_fields": [],
        "evidence_ids": [],
        "read_sha256": rules["read_ref"]["sha256"],
    }
    audit_path = root / "agent-gap-selection.json"

    def save_audit():
        audit_path.write_text(
            json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def project(request):
        selected = natural_gap_material_request(request, fields, multi_shipment=multi_shipment)
        if selected["requirements"] and selected["evidence"]:
            audit.update(
                state="selected",
                reason=None,
                requested_fields=[item["field_name"] for item in selected["requirements"]],
                evidence_ids=[item["evidence_id"] for item in selected["evidence"]],
            )
        return selected

    def adapter(request, *, timeout_seconds):
        audit["state"] = "requested"
        response = material_agent(request, timeout_seconds=timeout_seconds)
        response, unsent_requirements, unsent_evidence = restrict_material_response(
            response, request
        )
        audit.update(unsent_requirements=unsent_requirements, unsent_evidence=unsent_evidence)
        return response

    try:
        with scoped_material_evidence(evidence):
            result = run_main_channel(
                prepared["manifest_path"],
                p01,
                expected_task_id=manifest["task_id"],
                expected_request_fingerprint=prepared["request_fingerprint"],
                intake_context=prepared["intake_context"],
                selected_material_ids={target["material_id"]},
                read_configurations=configurations,
                reader_versions=versions,
                field_profile=profile,
                field_contract_ref=contract,
                output_dir=root / "main-channel",
                reader=lambda *_args, **_kwargs: copy.deepcopy(read),
                worker_fn=worker,
                extraction_agent=extraction_agent,
                material_agent=adapter,
                material_request_projector=project,
                markdown_provider=lambda *_args: sidecar,
                agent_read_fallback=True,
                agent_timeout_seconds=15,
            )
        if result["read_ref"]["sha256"] != rules["read_ref"]["sha256"]:
            raise ValueError("Material Agent did not reuse the rules READ")
        return {
            **result,
            **{key: rules[key] for key in ("word_supplement", "pdf_supplement") if key in rules},
        }
    finally:
        save_audit()


def _read_inline_image(root, record, output, *, python, cache, deadline):
    """Use the existing offline workbench OCR and attachment READ exporter."""
    if time.monotonic() >= deadline:
        return {"error": "task_timeout"}
    from local_attachment_markdown import encode, image_read

    from local_web import Workbench

    app = Workbench(output, python, cache)
    try:
        value = app.auxiliary("image_ocr", root, record["content"])
        if value.get("error"):
            return {"error": value["error"]}
        if not value.get("lines"):
            return {"error": "ocr_empty"}
        detail = {"type": "image", "lines": value["lines"], "size": value.get("size"), "fields": []}
        parts = image_read(detail)
        app.attachment_chain(
            root,
            record,
            output,
            "docling",
            detail,
            parts=parts,
            reader="RapidOCR",
            profile="local-image-cargo",
        )
        folder = output / ("attachment-" + record["material_id"])
        if "chain" in detail:
            path = folder / "read.json"
        else:
            # Keep the OCR evidence usable when only Markdown serialization fails.
            path = output / ("inline-ocr-read-" + record["material_id"] + ".json")
            path.write_bytes(
                encode(
                    {
                        "material_id": record["material_id"],
                        "input_sha256": record["content"]["sha256"],
                        "reader": "RapidOCR",
                        "parts": parts,
                    }
                )
            )
        reference = {"ref": str(path.resolve()), "sha256": _sha(path.read_bytes())}
        if "chain" in detail:
            for name, filename in (("markdown_ref", "content.md"), ("index_ref", "index.json")):
                extra = folder / filename
                reference[name] = {"ref": str(extra.resolve()), "sha256": _sha(extra.read_bytes())}
        return reference
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return {"error": "ocr_read_failed"}
    finally:
        app.pool.shutdown()
        app.group_pool.shutdown()


class LocalParser:
    def __init__(
        self,
        archive_root,
        output,
        docling_python,
        model_cache,
        index_path=None,
        *,
        pdf_document_timeout=45,
        pdf_supplement_timeout=45,
        task_timeout=180,
        body_input="original",
        extraction_agent=None,
        material_agent=None,
        pdf_ocr_page=None,
        markdown_worker=None,
        inline_image_ocr=False,
    ):
        self.archive_root = Path(archive_root)
        self.output = Path(output).resolve()
        self.docling_python = Path(docling_python)
        self.model_cache = Path(model_cache)
        self.index = LocalEmailIndex(index_path) if index_path is not None else None
        self.versions = None
        if body_input not in {"original", "markdown"}:
            raise ValueError("Unsupported local body input")
        if (
            extraction_agent is not None or material_agent is not None
        ) and body_input != "original":
            raise ValueError("The extraction Agent requires original body input")
        self.body_input = body_input
        self.extraction_agent = extraction_agent
        self.material_agent = material_agent
        self.markdown_worker = markdown_worker
        self.inline_image_ocr = inline_image_ocr
        if any(
            not isinstance(value, (int, float)) or value <= 0
            for value in (pdf_document_timeout, pdf_supplement_timeout, task_timeout)
        ):
            raise ValueError("PDF and task time budgets must be positive")
        self.pdf_document_timeout = pdf_document_timeout
        self.pdf_supplement_timeout = pdf_supplement_timeout
        self.task_timeout = task_timeout
        if pdf_ocr_page is not None and (type(pdf_ocr_page) is not int or pdf_ocr_page < 1):
            raise ValueError("PDF OCR page must be a positive integer")
        self.pdf_ocr_page = pdf_ocr_page

    def validate_request(self, value):
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("profile_id"), str)
            or value["profile_id"] not in INDEXED_PROFILES
        ):
            email_id, profile_id = _request(value)
            return "registered", email_id, profile_id
        profile_id = value["profile_id"]
        body = profile_id in CURRENT_BODY_PROFILES
        expected = (
            {"email_id", "profile_id"} if body else {"email_id", "attachment_id", "profile_id"}
        )
        if profile_id in {"indexed-pdf-row", "indexed-pdf-row-cargo"}:
            expected.add("row_anchor")
        if profile_id == ATTACHMENT_WEIGHT_PROFILE:
            expected.add("weight_table")
        if set(value) != expected:
            raise ValueError("Indexed request has missing or unsupported fields")
        if not all(
            isinstance(value[key], str) and value[key]
            for key in (("email_id",) if body else ("email_id", "attachment_id"))
        ):
            raise ValueError("Indexed identities must be nonempty strings")
        if profile_id in {"indexed-pdf-row", "indexed-pdf-row-cargo"} and (
            not isinstance(value["row_anchor"], str)
            or not value["row_anchor"].strip()
            or len(value["row_anchor"]) > 120
            or "\n" in value["row_anchor"]
            or "\r" in value["row_anchor"]
        ):
            raise ValueError(
                "row_anchor must be a nonempty single-line string of at most 120 characters"
            )
        if self.index is None:
            raise ValueError("Local attachment index is not configured")
        if profile_id == ATTACHMENT_WEIGHT_PROFILE:
            table_weight_object("indexed", value["weight_table"])
            if "page" not in value["weight_table"]:
                raise ValueError("The indexed attachment endpoint requires a PDF page selector")
        if body:
            if value["email_id"] not in self.index.emails:
                raise ValueError("Email is not present in the local index")
        else:
            self.index.select(value["email_id"], value["attachment_id"])
        return "indexed", value["email_id"], profile_id

    def parse(self, value):
        deadline = time.monotonic() + self.task_timeout
        mode, email_id, profile_id = self.validate_request(value)
        source = None
        if mode == "indexed":
            raw, source = read_indexed_email(
                self.index, self.archive_root, email_id, value.get("attachment_id")
            )
            kind = "eml"
        else:
            sample = SAMPLES[email_id]
            raw, kind = _input(sample, self.archive_root)
        run_id = "run-" + uuid4().hex
        root = self.output / run_id
        root.mkdir(parents=True, exist_ok=False)
        p01 = root / "p01"
        prepared = run_local_handoff(
            raw,
            p01,
            input_format=kind,
            request_reference="local:" + mode + ":" + email_id,
            task_id="local-" + uuid4().hex if kind == "eml" else None,
            email_ref_id=email_id if kind == "eml" else None,
            body_input=self.body_input if profile_id in CURRENT_BODY_PROFILES else "original",
        )
        manifest = prepared["received"]["manifest"]
        if source and profile_id in CURRENT_BODY_PROFILES:
            bodies = [
                item
                for item in manifest["materials"]
                if item["kind"] == "body" and item["state"] == "ready"
            ]
            if len(bodies) != 1:
                raise IndexedSourceError("P01 did not select one readable current body")
            target = bodies[0]
        else:
            target = _indexed_target(manifest, source) if source else _target(sample, manifest)
        worker = None
        pdf_support = None
        pdf_first_read = "native"
        versions = {}
        if kind == "eml":
            docling_worker = functools.partial(
                p02.worker, python=self.docling_python, model_cache=self.model_cache
            )
            if self.versions is None:
                self.versions = docling_worker(
                    {
                        "operation": "probe",
                        "hard_timeout_seconds": min(30, max(0.001, deadline - time.monotonic())),
                    }
                )
            versions = self.versions
            worker = docling_worker
            if target.get("format") == "pdf" and target["state"] == "ready":
                if self.pdf_ocr_page is None:
                    decision = p02.select_pdf_first_read(
                        p01,
                        target,
                        python=self.docling_python,
                        model_cache=self.model_cache,
                        timeout=deadline - time.monotonic(),
                    )
                    pdf_first_read = decision["route"]
                    (root / "pdf-first-read.json").write_text(
                        json.dumps(decision, ensure_ascii=False), encoding="utf-8"
                    )
                if pdf_first_read == "native" and (
                    self.pdf_ocr_page is not None or not p02.is_safety_sheet(target.get("filename"))
                ):
                    pdf_support = p02.pdf_supplement_support(
                        document_timeout=self.pdf_document_timeout,
                        supplement_timeout=self.pdf_supplement_timeout,
                        deadline=deadline,
                        ocr_page=self.pdf_ocr_page,
                    )
                else:

                    def worker(request):
                        remaining = max(0.001, deadline - time.monotonic())
                        if request["operation"] != "convert":
                            return docling_worker({**request, "hard_timeout_seconds": remaining})
                        return docling_worker(
                            {
                                **request,
                                "hard_timeout_seconds": remaining,
                                "profile": {
                                    **request["profile"],
                                    "document_timeout": min(self.pdf_document_timeout, remaining),
                                },
                            }
                        )

        agent_options = {}
        if (
            self.extraction_agent is not None or self.material_agent is not None
        ) and worker is not None:

            def export_worker(request):
                if self.markdown_worker is None:
                    return worker(request)
                remaining = deadline - time.monotonic()
                return self.markdown_worker(
                    {**request, "hard_timeout_seconds": max(0, min(45, remaining))}
                )

            agent_options = {
                "extraction_agent": self.extraction_agent,
                "material_agent": self.material_agent,
                "markdown_provider": functools.partial(
                    p02.export_read_markdown, worker_fn=export_worker
                ),
                "agent_read_fallback": True,
                "agent_timeout_seconds": 15,
            }
        configurations = p02.configurations(
            cache_path=self.model_cache,
            declared_versions=versions,
            pdf_first_read=pdf_first_read,
            **({"docling_text": True} if agent_options else {}),
        )
        contract = {
            "version": "0.1.0",
            "ref": str(FIELD_CONTRACT.resolve(strict=True)),
            "sha256": _sha(FIELD_CONTRACT.read_bytes()),
        }
        profile = (
            current_body_profile(manifest, target, profile_id)
            if profile_id in CURRENT_BODY_PROFILES
            else attachment_weight_profile(manifest, target, value["weight_table"])
            if profile_id == ATTACHMENT_WEIGHT_PROFILE
            else _indexed_profile(profile_id, manifest, target, value.get("row_anchor"))
            if source
            else _profile(profile_id, manifest, target)
        )
        material_agent = agent_options.get("material_agent")
        rules_options = (
            {
                key: option
                for key, option in agent_options.items()
                if key not in {"material_agent", "extraction_agent"}
            }
            if material_agent is not None
            else agent_options
        )
        result = run_main_channel(
            prepared["manifest_path"],
            p01,
            expected_task_id=manifest["task_id"],
            expected_request_fingerprint=prepared["request_fingerprint"],
            intake_context=prepared["intake_context"],
            selected_material_ids={target["material_id"]},
            read_configurations=configurations,
            reader_versions=versions,
            field_profile=profile,
            field_contract_ref=contract,
            output_dir=root
            / ("main-channel-rules" if material_agent is not None else "main-channel"),
            reader=p02.parse_prepared,
            worker_fn=worker,
            pdf_support=pdf_support,
            **rules_options,
        )
        if material_agent is not None and result.get("result_ref"):
            result = _material_gap_replay(
                result,
                root=root,
                p01=p01,
                prepared=prepared,
                manifest=manifest,
                target=target,
                profile=profile,
                configurations=configurations,
                versions=versions,
                contract=contract,
                worker=worker,
                material_agent=material_agent,
                extraction_agent=self.extraction_agent,
            )
        snapshot = read_validated_result(result["result_ref"], content_root=p01)
        read = read_result(result["read_ref"])
        response = _response(email_id, profile_id, run_id, result, snapshot, read, manifest)
        if self.inline_image_ocr and profile_id in CURRENT_BODY_PROFILES:
            projected = project_inline_company(
                response,
                manifest=manifest,
                target=target,
                content_root=p01,
                image_reader=lambda record: _read_inline_image(
                    p01,
                    record,
                    root,
                    python=self.docling_python,
                    cache=self.model_cache,
                    deadline=deadline,
                ),
            )
            if projected.get("inline_ocr", {}).get("state") == "accepted":
                augmented = publish_inline_company(
                    response, projected, content_root=p01, output_dir=root / "main-channel-inline"
                )
                if augmented is not None:
                    result = {**result, **augmented}
                    snapshot = read_validated_result(result["result_ref"], content_root=p01)
                    read = read_result(result["read_ref"])
                    response = _response(
                        email_id, profile_id, run_id, result, snapshot, read, manifest
                    )
                    response["inline_ocr"] = {
                        **projected["inline_ocr"],
                        "scope": "material_handoff",
                    }
                else:
                    response["inline_ocr"] = {
                        **projected["inline_ocr"],
                        "state": "not_applied",
                        "reason": "existing_result_requires_review",
                    }
            else:
                response = projected
        for key in ("word_supplement", "pdf_supplement"):
            if key in result:
                response[key] = result[key]
        if source:
            response["indexed_source"] = source
        with (root / "response.json").open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(response, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        return response


def make_handler(parser):
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status, body):
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            parsed = urlsplit(self.path)
            if parsed.path != "/local/index":
                self._reply(404, {"error": "not_found"})
                return
            if parser.index is None:
                self._reply(503, {"error": "index_not_configured"})
                return
            try:
                args = parse_qs(parsed.query, keep_blank_values=True)
                if set(args) - {"q", "offset", "limit"} or any(len(x) != 1 for x in args.values()):
                    raise ValueError("Index query parameters are invalid")
                query = args.get("q", [""])[0]
                offset = int(args.get("offset", ["0"])[0])
                limit = int(args.get("limit", ["20"])[0])
                if len(query) > 200 or offset < 0 or not 1 <= limit <= 100:
                    raise ValueError("Index query, offset or limit is outside its allowed range")
            except ValueError as error:
                self._reply(400, {"error": "invalid_request", "message": str(error)})
                return
            self._reply(200, parser.index.search(query, offset, limit))

        def do_POST(self):
            if self.path != "/local/parse":
                self._reply(404, {"error": "not_found"})
                return
            try:
                if self.headers.get_content_type() != "application/json":
                    raise ValueError("Content-Type must be application/json")
                size = int(self.headers.get("Content-Length", "-1"))
                if size < 0 or size > 4096:
                    raise ValueError("Content-Length must be between 0 and 4096")
                value = json.loads(self.rfile.read(size).decode("utf-8"))
                parser.validate_request(value)
            except (ValueError, UnicodeError, json.JSONDecodeError) as error:
                self._reply(400, {"error": "invalid_request", "message": str(error)})
                return
            try:
                response = parser.parse(value)
            except IndexedSourceError as error:
                self._reply(422, {"error": "indexed_source_unavailable", "message": str(error)})
                return
            except Exception as error:
                traceback.print_exc(file=sys.stderr)
                self._reply(500, {"error": "parse_failed", "type": type(error).__name__})
                return
            self._reply(200, response)

        def log_message(self, format, *args):
            print("local-parse: " + format % args, file=sys.stderr, flush=True)

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--docling-python", type=Path, default=p02.DOCLING_PYTHON)
    parser.add_argument("--model-cache", type=Path, default=p02.MODEL_CACHE)
    parser.add_argument("--index", type=Path, help="Local attachment inventory JSON")
    parser.add_argument("--body-input", choices=("original", "markdown"), default="original")
    parser.add_argument(
        "--extraction-agent",
        choices=("deepseek", "deepseek-narrow"),
        help="DeepSeek per material, or the narrow extraction route",
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--pdf-document-timeout", type=float, default=45)
    parser.add_argument("--pdf-supplement-timeout", type=float, default=45)
    parser.add_argument(
        "--pdf-ocr-page", type=int, help="Supplement this page of the selected PDF with local OCR"
    )
    parser.add_argument(
        "--inline-image-ocr",
        action="store_true",
        help="Use local OCR to supplement missing company from current inline images",
    )
    parser.add_argument("--task-timeout", type=float, default=180)
    parser.add_argument(
        "--markdown-workers",
        type=int,
        default=2,
        help="Offline Markdown process slots for Agent mode (1–16)",
    )
    args = parser.parse_args()
    if args.pdf_ocr_page is not None and args.pdf_ocr_page < 1:
        parser.error("--pdf-ocr-page must be a positive integer")
    if not 1 <= args.markdown_workers <= 16:
        parser.error("--markdown-workers must be between 1 and 16")
    agent = None
    if args.extraction_agent:
        if args.body_input != "original":
            parser.error("--extraction-agent requires --body-input original")
        try:
            agent = load_deepseek_agent()
        except ValueError as error:
            parser.error(str(error))
    pool = (
        p02.markdown_worker_pool(
            slots=args.markdown_workers, python=args.docling_python, model_cache=args.model_cache
        )
        if agent
        else None
    )
    service = LocalParser(
        args.archive_root,
        args.output,
        args.docling_python,
        args.model_cache,
        index_path=args.index,
        pdf_document_timeout=args.pdf_document_timeout,
        pdf_supplement_timeout=args.pdf_supplement_timeout,
        task_timeout=args.task_timeout,
        body_input=args.body_input,
        extraction_agent=agent if args.extraction_agent == "deepseek-narrow" else None,
        material_agent=agent.extract_material if args.extraction_agent == "deepseek" else None,
        pdf_ocr_page=args.pdf_ocr_page,
        markdown_worker=pool,
        inline_image_ocr=args.inline_image_ocr,
    )
    server = HTTPServer(("127.0.0.1", args.port), make_handler(service))
    print(f"Listening on http://127.0.0.1:{server.server_port}/local/parse", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if pool is not None:
            pool.close()


if __name__ == "__main__":
    main()
