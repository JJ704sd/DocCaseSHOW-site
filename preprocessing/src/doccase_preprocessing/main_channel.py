"""Local P01-to-material-result orchestration for a deliberately narrow field set.

The caller supplies the existing P02 reader, execution settings and an independent
field profile. This is a local integration slice, not a general document parser.
"""

import copy
import hashlib
import json
import math
import os
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal
from functools import partial
from pathlib import Path
from uuid import uuid4

from .agent_gap_evidence import (
    SHIPMENT_SUMMARY_FIELDS,
    compact_cargo_context,
    compact_gap_evidence,
    group_declaration_evidence,
    quote_table_gap_evidence,
    shipment_summary_evidence,
    split_cargo_gap_evidence,
)
from .cargo_table import CARGO_ROW_FIELDS, CARGO_ROW_PROFILE, cargo_row_object, packing_rows
from .extraction_agent import (
    FULL_CHAIN_INSTRUCTION,
    AgentResponseError,
    AgentServiceError,
    current_body_text_check,
    current_route_destinations,
    mapped_markdown_fragments,
    markdown_evidence,
    parse_response,
    revised_count_supported,
)
from .inline_company import (
    _box,
    _company_lines,
    eligible_inline_images,
    sender_domain,
    signature_section_evidence,
)
from .inquiry_details import (
    AGENT_DETAIL_FIELDS,
    DETAIL_FIELDS,
    RESPONSIBILITY,
    agent_body_detail_proofs,
    declared_shipment_terms,
    inquiry_body_details,
)
from .local_handoff import _read_content
from .markdown_body import mapped_sections, validate_mapping
from .material_agent import material_candidates, material_evidence
from .pdf_profile import PDF_READERS, PDF_SCANNED, PDF_STRUCTURED_READERS, pdf_page_title
from .rules import evaluate_candidates
from .weight_table import table_weight, table_weight_object

_MATERIAL_EVIDENCE_SCOPE = ContextVar("material_evidence_scope", default=None)


@contextmanager
def scoped_material_evidence(provider):
    """Bind a verified evidence projection to one main-channel invocation."""
    token = _MATERIAL_EVIDENCE_SCOPE.set(provider)
    try:
        yield
    finally:
        _MATERIAL_EVIDENCE_SCOPE.reset(token)


_NUMBER = r"(?P<number>\d+(?:\.\d+)?)"
_COUNT = re.compile(rf"(?<![\w.]){_NUMBER}\s*(?:cartons?|ctns?|箱)(?:\s+boxes)?(?![A-Za-z])", re.I)
_PACKAGE_TOTAL = re.compile(
    rf"\btotal\s+packages?\s*[:：]\s*{_NUMBER}\s*(?:packages?|pallets?|cartons?|ctns?)\b", re.I
)
_WEIGHT = re.compile(
    rf"(?P<scope>total|line|each|per\s+(?:carton|box)|总|整行|每箱)\s*"
    rf"(?P<kind>g\s*\.?\s*w\s*\.?|g\s*\.|n\s*\.?\s*w\s*\.?|gross\s*(?:weight)?|"
    rf"net\s*(?:weight)?|毛重|净重)\s*[:：]?\s*{_NUMBER}\s*"
    r"(?P<unit>kg|kgs|kilograms?|g|grams?|lb|lbs|pounds?|千克|公斤|克|磅)(?![A-Za-z])",
    re.I,
)
_CHARGEABLE_WEIGHT = re.compile(
    r"(?:chargeable\s+weight|计费重(?:量)?)\s*[:：]?\s*(?P<number>\d+(?:\.\d+|,\d{1,2})?)\s*"
    r"(?P<unit>kg|kgs|kilograms?|g|grams?|lb|lbs|pounds?|千克|公斤|克|磅)(?![A-Za-z])",
    re.I,
)
_VOLUME = re.compile(
    rf"(?:total\s+volume|line\s+volume|总体积|整行体积)\s*[:：]?\s*{_NUMBER}\s*"
    r"(?P<unit>cbm|m³|m3)(?![A-Za-z])",
    re.I,
)
_CARGO_VOLUME = re.compile(
    rf"\bcargo\s+details\s*:\s*[^\r\n]*?{_NUMBER}\s*"
    r"(?P<unit>cbm|m³|m3)(?![A-Za-z])",
    re.I,
)
_VOLUME_TOKEN = re.compile(r"(?<![\w.])\d+(?:\.\d+)?\s*(?:cbm|m³|m3)(?![A-Za-z])", re.I)
_EQUAL = re.compile(r"all\s+cartons\s+have\s+(?:the\s+)?same\s+weight|每箱(?:的)?重量相同", re.I)
_INQUIRY_LABELS = {
    "destination": r"destinati[oó]n(?:\s+(?:port|airport))?|destino|port\s+of\s+destination|目的地|目的港",
    "origin": r"(?:cargo\s+)?origin|place\s+of\s+origin|货源地|起运地",
    "trade_terms": r"incoterms?|trade\s+terms?|terms?|贸易条款",
    "cargo_ready_time": r"cargo\s+(?:will\s+be\s+)?ready(?:\s+(?:date|time))?|ready\s+date|货好时间",
    "customer_company": r"customer\s+company|客户公司(?:名|名称)?",
    "contact_name": r"contact\s+(?:person|name)|联系人(?:姓名)?",
    "contact_phone": r"contact\s+(?:phone|telephone|tel)|联系电话",
    "signature_email": r"signature\s+e-?mail|签名邮箱",
    "salesperson": r"salesperson|sales\s+representative|业务员",
    "internal_reference": r"internal\s+reference|our\s+reference|需求单号",
    "cargo_type": r"cargo\s+type|货物类型",
    "cargo_description": r"commodity|cargo\s+description|货物品名",
    "business_remarks": r"remarks?|备注",
}
INQUIRY_FIELDS = (*_INQUIRY_LABELS, "chargeable_weight")
LOCATION_INQUIRY_PROFILE = "local-current-body-inquiry-locations"
WEIGHT_INQUIRY_PROFILE = "local-current-body-inquiry-weights"
ATTACHMENT_WEIGHT_PROFILE = "local-attachment-weight-table"
SHIPMENT_WEIGHT_FIELDS = (
    "shipment_net_weight",
    "shipment_gross_weight",
    "shipment_chargeable_weight",
)
BODY_WEIGHT_FIELDS = (*SHIPMENT_WEIGHT_FIELDS, "unit_net_weight", "unit_gross_weight")
ROW_WEIGHT_FIELDS = (
    "line_net_weight",
    "line_gross_weight",
    "line_chargeable_weight",
    "unit_net_weight",
    "unit_gross_weight",
)
_PICKUP_ADDRESS_LABEL = r"pick[ \t-]*up[ \t]+(?:address|location)|提货地址|提货地点"
_LOCATION_LABELS = {
    "cargo_origin": r"cargo[ \t]+origin|place[ \t]+of[ \t]+origin|ship[ \t]+from|货源地|发货地",
    "pickup_city": rf"pick[ \t-]*up[ \t]+city|提货城市|{_PICKUP_ADDRESS_LABEL}",
    "departure_port": (
        r"(?:origin|departure)[ \t]+(?:airport|port)|(?:airport|port)[ \t]+of[ \t]+"
        r"(?:loading|departure)|AOL|POL|起运港|装货港|起飞机场|起运机场"
    ),
}
SPLIT_INQUIRY_FIELDS = (*_LOCATION_LABELS, *(name for name in INQUIRY_FIELDS if name != "origin"))
_INQUIRY_LABELS.update(_LOCATION_LABELS)
_INCOTERM = re.compile(r"\b(?:EXW|FCA|FAS|FOB|CFR|CIF|CPT|CIP|DAP|DPU|DDP)\b", re.I)
_COMPACT_CARGO_SUMMARY = re.compile(
    rf"(?:cargo[ \t]+details[ \t]*[:：]|(?:{_INCOTERM.pattern}|CNF)[ \t]*[,，])"
    r"[ \t]*[1-9]\d*[ \t]*(?:cartons?|ctns?)(?:[ \t]*[,，][ \t]*|[ \t]+)"
    r"\d+(?:\.\d+)?[ \t]*kgs?(?:[ \t]*[,，][ \t]*|[ \t]+)"
    r"(?P<volume>\d+(?:\.\d+)?)[ \t]*(?:cbm|m³|m3)[ \t]*\.?",
    re.I,
)
_FIELDS = {
    "ctns",
    "line_gross_weight",
    "line_net_weight",
    "unit_gross_weight",
    "unit_net_weight",
    "line_volume",
}
_FIELD_SEMANTICS = {
    "ctns": ("line_total", "package_count"),
    "line_gross_weight": ("line_total", "gross_weight"),
    "line_net_weight": ("line_total", "net_weight"),
    "unit_gross_weight": ("per_item", "gross_weight"),
    "unit_net_weight": ("per_item", "net_weight"),
    "line_volume": ("line_total", "volume"),
}
_CARGO_TYPES = {
    "product_name": "text_field",
    "package_type": "package_field",
    "package_count": "integer_field",
    "length_cm": "cm_field",
    "width_cm": "cm_field",
    "height_cm": "cm_field",
    "raw_observations": "text_field",
}
_FIELDS.update(_CARGO_TYPES)
_FIELDS.update(SPLIT_INQUIRY_FIELDS)
_FIELDS.update(INQUIRY_FIELDS)
_FIELDS.update((*SHIPMENT_WEIGHT_FIELDS, "line_chargeable_weight"))
_FIELD_SEMANTICS.update(
    {name: ("document", name.removeprefix("shipment_")) for name in SHIPMENT_WEIGHT_FIELDS}
)
_FIELD_SEMANTICS["line_chargeable_weight"] = ("line_total", "chargeable_weight")
_FIELD_SEMANTICS.update(
    {
        "product_name": ("line_total", "product_name"),
        "package_type": ("line_total", "package_type"),
        "package_count": ("line_total", "package_count"),
        "length_cm": ("per_item", "length"),
        "width_cm": ("per_item", "width"),
        "height_cm": ("per_item", "height"),
        "raw_observations": ("line_total", "raw_observation"),
    }
)
_FIELD_SEMANTICS.update(
    {name: ("document", name) for name in {*INQUIRY_FIELDS, *SPLIT_INQUIRY_FIELDS}}
)


def _field_type(name):
    if name in _INQUIRY_LABELS:
        return "text_field"
    return _CARGO_TYPES.get(name) or (
        "integer_field" if name == "ctns" else "cbm_field" if name == "line_volume" else "kg_field"
    )


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _save(path, value):
    """Write only within this new run directory, then atomically publish the file."""
    path = Path(path)
    raw = _json_bytes(value)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {"ref": str(path.resolve()), "sha256": _sha(raw)}


def read_result(reference):
    """Read back a fixed local snapshot or fail if its bytes changed."""
    raw = Path(reference["ref"]).read_bytes()
    if _sha(raw) != reference["sha256"]:
        raise ValueError("Saved result SHA-256 mismatch")
    return json.loads(raw)


def _profile(profile, manifest):
    if profile is None:
        return None
    profile = copy.deepcopy(profile)
    if (
        not isinstance(profile, dict)
        or not profile.get("id")
        or not profile.get("version")
        or not profile.get("task_scope_ref")
    ):
        raise ValueError("Field profile must have an id and version")
    requirements = profile.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        raise ValueError("Field profile must contain independent requirements")
    ids = set()
    materials = {item["material_id"]: item for item in manifest["materials"]}
    for requirement in requirements:
        rid = requirement.get("requirement_id")
        material_id = requirement.get("material_id")
        if not rid or rid in ids or material_id not in materials:
            raise ValueError("Requirement identity or material is invalid")
        if requirement.get("field_name") not in _FIELDS or not requirement.get("object_selector"):
            raise ValueError("Unsupported field or missing object selector")
        if requirement["field_name"] in {*INQUIRY_FIELDS, *SPLIT_INQUIRY_FIELDS}:
            profile_fields = {
                "local-current-body-inquiry": INQUIRY_FIELDS,
                LOCATION_INQUIRY_PROFILE: SPLIT_INQUIRY_FIELDS,
                WEIGHT_INQUIRY_PROFILE: tuple(
                    name for name in SPLIT_INQUIRY_FIELDS if name != "chargeable_weight"
                ),
            }
            if (
                requirement["field_name"] not in profile_fields.get(profile["id"], ())
                or requirement.get("source_kind") != "current_body"
            ):
                raise ValueError("Inquiry fields require the matching current-body inquiry profile")
        if (
            requirement["field_name"] in _LOCATION_LABELS
            and requirement["object_selector"] != material_id + "/document"
        ):
            raise ValueError("Split location fields require the current inquiry document")
        cargo_row = requirement.get("cargo_table_row")
        if profile["id"] == CARGO_ROW_PROFILE or "cargo_table_row" in requirement:
            expected = cargo_row_object(material_id, cargo_row)
            record = materials[material_id]
            if (
                profile["id"] != CARGO_ROW_PROFILE
                or not (record["kind"] == "attachment" or _is_named_inline_pdf(record))
                or record["format"] != ("pdf" if "page" in cargo_row else "xlsx")
                or requirement["source_kind"] != "attachment"
                or requirement["object_selector"] != expected
                or requirement["field_name"] not in CARGO_ROW_FIELDS
                or any(
                    k in requirement
                    for k in (
                        "object_anchor",
                        "source_pattern",
                        "pdf_row_anchor",
                        "pdf_cargo",
                        "pdf_table_total",
                        "pdf_split_total",
                        "pdf_total_volume",
                        "weight_table",
                    )
                )
            ):
                raise ValueError("Cargo row scope does not match the material, field or object")
        if profile["id"] == ATTACHMENT_WEIGHT_PROFILE or "weight_table" in requirement:
            selector = requirement.get("weight_table")
            expected_object = table_weight_object(material_id, selector)
            record = materials[material_id]
            if (
                profile["id"] != ATTACHMENT_WEIGHT_PROFILE
                or record["kind"] != "attachment"
                or record["format"] != ("pdf" if "page" in selector else "xlsx")
                or requirement.get("source_kind") != "attachment"
                or requirement["object_selector"] != expected_object
                or requirement["field_name"]
                not in (ROW_WEIGHT_FIELDS if "row" in selector else SHIPMENT_WEIGHT_FIELDS)
                or any(
                    requirement.get(k)
                    for k in (
                        "pdf_row_anchor",
                        "pdf_cargo",
                        "pdf_table_total",
                        "pdf_split_total",
                        "pdf_total_volume",
                        "object_anchor",
                        "source_pattern",
                    )
                )
            ):
                raise ValueError("Weight table scope does not match the material, field or object")
        if (
            profile["id"] == WEIGHT_INQUIRY_PROFILE
            and requirement["field_name"]
            in {
                *BODY_WEIGHT_FIELDS,
                *ROW_WEIGHT_FIELDS,
            }
            and (
                requirement["field_name"] not in BODY_WEIGHT_FIELDS
                or requirement["source_kind"] != "current_body"
                or requirement["object_selector"] != material_id + "/document"
            )
        ):
            raise ValueError("Scoped inquiry weight must belong to the current document")
        if requirement["field_name"] in {
            *SHIPMENT_WEIGHT_FIELDS,
            "line_chargeable_weight",
        } and profile["id"] not in {WEIGHT_INQUIRY_PROFILE, ATTACHMENT_WEIGHT_PROFILE}:
            raise ValueError("Scoped weight needs an explicit weight profile")
        cargo = requirement.get("pdf_cargo")
        if cargo is not None and (
            cargo is not True
            or profile["id"] not in {"indexed-pdf-row-cargo", "indexed-pdf-document-cargo"}
            or requirement["field_name"] not in {*_CARGO_TYPES, "ctns"}
            or requirement.get("source_kind") != "attachment"
            or bool(requirement.get("pdf_row_anchor")) != (profile["id"] == "indexed-pdf-row-cargo")
            or requirement["object_selector"]
            != material_id + ("/selected-row" if requirement.get("pdf_row_anchor") else "/document")
        ):
            raise ValueError("Invalid local PDF cargo scope")
        if (
            requirement["field_name"] in _CARGO_TYPES
            and cargo is not True
            and not cargo_row
            and not (
                requirement.get("source_kind") == "current_body"
                and (
                    requirement["field_name"] == "package_count"
                    or (
                        requirement["field_name"] == "package_type"
                        and profile["id"]
                        in {
                            "local-current-body-inquiry",
                            LOCATION_INQUIRY_PROFILE,
                            WEIGHT_INQUIRY_PROFILE,
                        }
                    )
                )
            )
        ):
            raise ValueError("Local cargo field needs an explicit PDF cargo scope")
        for key in ("object_anchor", "source_pattern"):
            if key in requirement and (
                not isinstance(requirement[key], str) or not requirement[key].strip()
            ):
                raise ValueError("Field profile scope selector must be a nonempty string")
        if "source_pattern" in requirement:
            try:
                re.compile(requirement["source_pattern"], re.I)
            except re.error as error:
                raise ValueError("Field profile scope selector is not a valid pattern") from error
        if requirement.get("source_kind") not in {"current_body", "quoted_body", "attachment"}:
            raise ValueError("Requirement must declare its evidence section kind")
        if "pdf_total_volume" in requirement and (
            requirement["pdf_total_volume"] is not True
            or requirement["field_name"] != "line_volume"
            or requirement["source_kind"] != "attachment"
            or requirement.get("object_anchor")
            or requirement.get("source_pattern")
        ):
            raise ValueError("PDF total-volume scope must select an unambiguous attachment total")
        if "pdf_split_total" in requirement and (
            requirement["pdf_split_total"] is not True
            or requirement["field_name"] not in {"line_gross_weight", "line_volume"}
            or requirement["source_kind"] != "attachment"
            or requirement["object_selector"] != material_id + "/document"
            or requirement.get("object_anchor")
            or requirement.get("source_pattern")
            or requirement.get("pdf_row_anchor")
            or requirement.get("pdf_total_volume")
        ):
            raise ValueError("Split PDF total must select an unambiguous attachment total")
        if "pdf_table_total" in requirement and (
            requirement["pdf_table_total"] is not True
            or requirement["field_name"]
            not in {"ctns", "line_net_weight", "line_gross_weight", "line_volume"}
            or requirement["source_kind"] != "attachment"
            or requirement["object_selector"] != material_id + "/document"
            or requirement.get("pdf_row_anchor")
        ):
            raise ValueError("Table total must select an unambiguous attachment document")
        if "pdf_row_anchor" in requirement and (
            not isinstance(requirement["pdf_row_anchor"], str)
            or not requirement["pdf_row_anchor"].strip()
            or requirement["source_kind"] != "attachment"
            or requirement.get("object_anchor")
            or requirement.get("source_pattern")
            or requirement.get("pdf_total_volume")
        ):
            raise ValueError("PDF row scope must name an unambiguous attachment row")
        if requirement["source_kind"] == "quoted_body" and not requirement.get(
            "quoted_section_markers"
        ):
            raise ValueError("Quoted body requires independent section markers")
        if not requirement["object_selector"].startswith(material_id + "/"):
            raise ValueError("Object selector must belong to its material")
        if requirement.get("missing_policy") not in {"allow", "block"}:
            raise ValueError("Requirement must explicitly declare its missing policy")
        if not isinstance(requirement.get("critical"), bool):
            raise ValueError("Requirement must explicitly declare criticality")
        if not isinstance(requirement.get("allow_not_applicable"), bool):
            raise ValueError("Requirement must explicitly declare applicability policy")
        dependencies = requirement.get("scope_dependencies")
        valid_refs = {material_id + "/representation" for material_id in materials}
        if (
            not isinstance(dependencies, list)
            or not dependencies
            or any(
                dependency.get("stage")
                not in {"acquisition", "parsing", "extraction", "aggregation"}
                or not dependency.get("refs")
                or not set(dependency["refs"]).issubset(valid_refs)
                for dependency in dependencies
            )
        ):
            raise ValueError("Requirement must declare independent material scope dependencies")
        ids.add(rid)
    return profile


def _blocks(execution):
    ref = execution["ref"]
    raw = Path(ref).read_bytes()
    if _sha(raw) != execution["artifact_hash"]:
        raise ValueError("READ artifact SHA-256 mismatch")
    document = json.loads(raw)
    if execution["executed_profile"]["reader"] == "TextReader":
        return document, [{"pointer": "/text", "text": document["text"], "provenance": []}]
    blocks = []
    if document.get("kind") == "docling_pdf_read_bundle":
        documents = []
        for part_index, part in enumerate(document["parts"]):
            part_raw = Path(part["ref"]).read_bytes()
            if _sha(part_raw) != part["sha256"] or json.loads(part_raw) != part["document"]:
                raise ValueError("PDF pass artifact or embedded document differs")
            documents.append((f"/parts/{part_index}/document", part["document"], part["kind"]))
    else:
        documents = [
            (
                "",
                document,
                "ocr" if execution["executed_profile"]["reader"] == PDF_SCANNED else None,
            )
        ]
    for prefix, observed, pdf_pass in documents:
        first_block = len(blocks)
        for index, item in enumerate(observed.get("texts", [])):
            if isinstance(item.get("text"), str):
                block = {
                    "pointer": f"{prefix}/texts/{index}/text",
                    "text": item["text"],
                    "provenance": item.get("prov", []),
                }
                if pdf_pass == "ocr":
                    block["pdf_pass"] = pdf_pass
                parent = (item.get("parent") or {}).get("$ref", "")
                group = re.fullmatch(r"#/groups/(\d+)", parent)
                if group and int(group.group(1)) < len(observed.get("groups", [])):
                    if observed["groups"][int(group.group(1))].get("label") == "inline":
                        block["inline_group"] = prefix + parent
                blocks.append(block)
        for table_index, table in enumerate(observed.get("tables", [])):
            for cell_index, cell in enumerate(table.get("data", {}).get("table_cells", [])):
                if isinstance(cell.get("text"), str):
                    blocks.append(
                        {
                            "pointer": f"{prefix}/tables/{table_index}/data/table_cells/{cell_index}/text",
                            "text": cell["text"],
                            "provenance": table.get("prov", []),
                            "cell": {
                                key: cell.get(key)
                                for key in (
                                    "start_row_offset_idx",
                                    "end_row_offset_idx",
                                    "start_col_offset_idx",
                                    "end_col_offset_idx",
                                    "column_header",
                                    "row_header",
                                )
                            },
                        }
                    )
        if execution["executed_profile"]["reader"] in {
            "DoclingHtml",
            "DoclingMarkdown",
        } and observed.get("body", {}).get("children"):
            by_ref = {}
            for block in blocks[first_block:]:
                pointer = block["pointer"][len(prefix) :]
                ref = (
                    "#" + pointer.split("/data/")[0]
                    if "/data/" in pointer
                    else "#" + pointer.rsplit("/", 1)[0]
                )
                by_ref.setdefault(ref, []).append(block)
            ordered, visited = [], set()
            pending = list(reversed(observed["body"]["children"]))
            while pending:
                ref = pending.pop()["$ref"]
                if ref in visited:
                    continue
                visited.add(ref)
                group = re.fullmatch(r"#/groups/(\d+)", ref)
                if group:
                    pending.extend(
                        reversed(observed["groups"][int(group.group(1))].get("children", []))
                    )
                else:
                    ordered.extend(by_ref.get(ref, []))
            used = {id(block) for block in ordered}
            blocks[first_block:] = ordered + [
                block for block in blocks[first_block:] if id(block) not in used
            ]
    return document, blocks


def _markdown_blocks(execution, record, records, content_root):
    blocks = _blocks(execution)[1]
    if execution["executed_profile"]["reader"] != "DoclingMarkdown":
        return blocks
    conversion = record["metadata"]["markdown_conversion"]
    parent = records[record["parent_material_id"]]
    root = Path(content_root).resolve(strict=True)
    mapping = json.loads(_read_content(root, conversion["mapping"]))
    validate_mapping(
        _read_content(root, parent["content"]), _read_content(root, record["content"]), mapping
    )
    sections = mapped_sections([block["text"] for block in blocks], mapping)
    for block, section in zip(blocks, sections, strict=True):
        block["markdown_section"] = section
    return blocks


def _positioned_pdf_blocks(blocks):
    pages = {}
    for block in blocks:
        if not re.fullmatch(r"(?:/parts/\d+/document)?/texts/\d+/text", block["pointer"]):
            continue
        provenance = block.get("provenance", [])
        if len(provenance) != 1:
            continue
        item = provenance[0]
        box = item.get("bbox", {})
        values = [box.get(key) for key in ("l", "t", "r", "b")]
        if (
            not isinstance(item.get("page_no"), int)
            or box.get("coord_origin") != "BOTTOMLEFT"
            or any(not isinstance(x, (int, float)) or not math.isfinite(x) for x in values)
            or box["l"] >= box["r"]
            or box["b"] >= box["t"]
        ):
            continue
        pages.setdefault(item["page_no"], []).append((block, box))
    return pages


def _overlaps_x(first, second):
    return max(first["l"], second["l"]) < min(first["r"], second["r"])


def _overlaps_y(first, second):
    return max(first["b"], second["b"]) < min(first["t"], second["t"])


_PDF_SPLIT_TOTAL_LABELS = {
    "line_gross_weight": re.compile(r"total\s+gross\s+weight\s*[:：]?", re.I),
    "line_volume": re.compile(r"total\s+volume\s*[:：]?", re.I),
}
_PDF_SPLIT_TOTAL_VALUES = {
    "line_gross_weight": re.compile(
        r"\s*\d+(?:\.\d+)?\s*(?:kgs?|kilograms?|g|grams?|lbs?|pounds?|千克|公斤|克|磅)"
        r"(?:\s*\([^()\d]*\))?\s*",
        re.I,
    ),
    "line_volume": re.compile(r"\s*\d+(?:\.\d+)?\s*(?:cbm|m³|m3)(?:\s*\([^()\d]*\))?\s*", re.I),
}


def _pdf_split_total(blocks, field_name, *, evidence_only=False):
    """Bind one document-total label to one adjacent value on the same PDF row."""
    label_pattern = _PDF_SPLIT_TOTAL_LABELS[field_name]
    labels = [
        block
        for block in blocks
        if re.fullmatch(r"(?:/parts/\d+/document)?/texts/\d+/text", block["pointer"])
        and label_pattern.fullmatch(block["text"].strip())
    ]
    if len(labels) != 1:
        return None
    label = labels[0]
    pages = _positioned_pdf_blocks(blocks)
    located = [
        (entries, box)
        for entries in pages.values()
        for block, box in entries
        if block["pointer"] == label["pointer"]
    ]
    if len(located) != 1:
        return None
    entries, label_box = located[0]

    def same_row(box):
        overlap = min(label_box["t"], box["t"]) - max(label_box["b"], box["b"])
        return overlap >= 0.5 * min(label_box["t"] - label_box["b"], box["t"] - box["b"])

    value_pattern = _PDF_SPLIT_TOTAL_VALUES[field_name]
    if evidence_only and field_name == "line_gross_weight":
        value_pattern = re.compile(
            value_pattern.pattern.replace(
                r"\d+(?:\.\d+)?", r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", 1
            ),
            re.I,
        )
    candidates = [
        (block, box)
        for block, box in entries
        if box["l"] >= label_box["r"]
        and same_row(box)
        and (
            not evidence_only
            or block["pointer"].split("/texts/", 1)[0] == label["pointer"].split("/texts/", 1)[0]
        )
        and value_pattern.fullmatch(block["text"])
    ]
    if len(candidates) != 1:
        return None
    value_block, value_box = candidates[0]
    if any(
        block["pointer"] not in {label["pointer"], value_block["pointer"]}
        and block["text"].strip()
        and same_row(box)
        and box["r"] > label_box["r"]
        and box["l"] < value_box["l"]
        for block, box in entries
    ):
        return None
    value_text = value_block["text"].replace(",", "") if evidence_only else value_block["text"]
    facts = list(_facts(field_name, label["text"] + " " + value_text))
    if len(facts) != 1:
        return None
    _, value, unit = facts[0]
    return value, unit, [value_block, label]


def _pdf_total_volume(blocks):
    """Find one total-row CBM value using native PDF page coordinates."""
    pages = _positioned_pdf_blocks(blocks)

    matches = []
    for entries in pages.values():
        rows = [(block, box) for block, box in entries if block["text"].strip().upper() == "TOTAL:"]
        units = [(block, box) for block, box in entries if block["text"].strip().upper() == "CBM"]
        if len(rows) != 1 or len(units) != 1:
            continue
        row, row_box = rows[0]
        unit, unit_box = units[0]
        headers = [
            (block, box)
            for block, box in entries
            if block["text"].strip().upper() in {"TOTAL", "CBM TOTAL"}
            and _overlaps_x(box, unit_box)
            and (
                0 <= box["b"] - unit_box["t"] <= 2 * (box["t"] - box["b"])
                or (block["text"].strip().upper() == "CBM TOTAL" and _overlaps_y(box, unit_box))
            )
        ]
        if len(headers) != 1 or row_box["r"] >= unit_box["l"] or row_box["t"] >= unit_box["b"]:
            continue
        values = [
            (block, box)
            for block, box in entries
            if re.fullmatch(r"\d+(?:\.\d+)?", block["text"].strip())
            and _overlaps_x(box, unit_box)
            and _overlaps_y(box, row_box)
        ]
        other_rows = [
            block
            for block, box in entries
            if re.fullmatch(r"\d+(?:\.\d+)?", block["text"].strip())
            and _overlaps_x(box, unit_box)
            and row_box["t"] < box["b"] < box["t"] < unit_box["b"]
        ]
        if len(values) == 1 and other_rows:
            value_block = values[0][0]
            matches.append(
                (
                    _decimal_value(value_block["text"].strip()),
                    [value_block, headers[0][0], unit, row],
                )
            )
    return matches[0] if len(matches) == 1 else None


def _pdf_unlabeled_carton_total(cells, field_name):
    """Recognize an unlabeled final row only when carton ranges and totals agree."""
    carton_headers = [
        b for b in cells if re.fullmatch(r"\s*carton\s*(?:no\.?|number)\s*", b["text"], re.I)
    ]
    if len(carton_headers) != 1:
        return []
    carton_header = carton_headers[0]
    header_row = carton_header["cell"]["start_row_offset_idx"]
    header_cells = [b for b in cells if b["cell"]["start_row_offset_idx"] == header_row]
    packages = [b for b in header_cells if re.fullmatch(r"\s*packages?\s*", b["text"], re.I)]
    if len(packages) != 1:
        return []
    package_header = packages[0]
    carton_col = carton_header["cell"]["start_col_offset_idx"]
    package_col = package_header["cell"]["start_col_offset_idx"]
    last_row = max(b["cell"]["start_row_offset_idx"] for b in cells)

    def at(row, col):
        matches = [
            b
            for b in cells
            if b["cell"]["start_row_offset_idx"] == row and b["cell"]["start_col_offset_idx"] == col
        ]
        return matches[0] if len(matches) == 1 else None

    if last_row <= header_row + 2 or at(last_row, carton_col) is not None:
        return []
    total_count = at(last_row, package_col)
    if not total_count or not total_count["text"].strip().isdigit():
        return []
    expected_start, row_evidence = 1, []
    rows = range(header_row + 1, last_row)
    for row in rows:
        carton, count = at(row, carton_col), at(row, package_col)
        if not carton or not count or not count["text"].strip().isdigit():
            return []
        match = re.fullmatch(r"\s*(\d+)\s*(?:[-–]\s*(\d+))?\s*", carton["text"])
        if not match:
            return []
        first, last = int(match.group(1)), int(match.group(2) or match.group(1))
        if (
            first != expected_start
            or last < first
            or int(count["text"].strip()) != last - first + 1
        ):
            return []
        expected_start = last + 1
        row_evidence.extend([carton, count])
    if expected_start - 1 != int(total_count["text"].strip()):
        return []
    if field_name in {"ctns", "package_count"}:
        return [
            (expected_start - 1, None, [total_count, package_header, carton_header, *row_evidence])
        ]
    pattern = {
        "line_net_weight": r"\bn\s*[./]?\s*w\b|net\s*weight",
        "line_gross_weight": r"\bg\s*[./]?\s*w\b|gross\s*weight",
    }.get(field_name)
    if not pattern:
        return []
    headers = [b for b in header_cells if re.search(pattern, b["text"], re.I)]
    if len(headers) != 1:
        return []
    header = headers[0]
    col = header["cell"]["start_col_offset_idx"]
    values = [at(row, col) for row in range(header_row + 1, last_row + 1)]
    units = [at(row, col + 1) for row in range(header_row + 1, last_row + 1)]
    if any(
        not value or not re.fullmatch(r"\d+(?:\.\d+)?", value["text"].strip()) for value in values
    ):
        return []
    if any(not unit or unit["text"].strip().lower() not in {"kg", "kgs"} for unit in units):
        return []
    numbers = [Decimal(value["text"].strip()) for value in values]
    if sum(numbers[:-1]) != numbers[-1]:
        return []
    return [
        (
            _decimal_value(numbers[-1]),
            "kg",
            [
                values[-1],
                header,
                units[-1],
                total_count,
                package_header,
                carton_header,
                *row_evidence,
                *values[:-1],
            ],
        )
    ]


def _pdf_total_with_item_quantity(cells):
    """Recognize a merged leading TOTAL cell whose number is explicitly in the pieces column."""
    final_row = max(b["cell"]["start_row_offset_idx"] for b in cells)
    matches = []
    for block in cells:
        row = block["cell"]
        if (
            row["start_row_offset_idx"] != final_row
            or row["end_row_offset_idx"] != final_row + 1
            or row["start_col_offset_idx"] != 0
            or row["end_col_offset_idx"] <= 1
            or not re.fullmatch(r"total\s*[:：]\s*\d+", block["text"].strip(), re.I)
        ):
            continue
        end = row["end_col_offset_idx"]
        headers = sorted(
            (
                b
                for b in cells
                if b["cell"].get("column_header")
                and b["cell"]["end_row_offset_idx"] <= final_row
                and b["cell"]["start_col_offset_idx"] == end - 1
                and b["cell"]["end_col_offset_idx"] == end
            ),
            key=lambda b: b["cell"]["start_row_offset_idx"],
        )
        if not 1 <= len(headers) <= 2 or (
            len(headers) == 2
            and headers[0]["cell"]["end_row_offset_idx"]
            != headers[1]["cell"]["start_row_offset_idx"]
        ):
            continue
        heading = re.sub(r"\s+", "", "".join(b["text"] for b in headers))
        if re.fullmatch(r"(?:quantity|qty)[(（](?:pcs|pieces)[)）]", heading, re.I):
            matches.append((block, headers))
    return matches[0] if len(matches) == 1 else None


def _pdf_decimal_volume_footer(cells, total_row, value, heading):
    """Resolve a decimal comma only when the same table's total confirms it."""
    number = re.fullmatch(r"\s*(\d+,\d{1,4})\s*", value)
    if not number or not re.fullmatch(r"\s*(?:total\s+)?(?:cbm|m\s*[³3])\s*", heading, re.I):
        return None
    last_row = max(b["cell"]["end_row_offset_idx"] for b in cells) - 1
    width = max(b["cell"]["end_col_offset_idx"] for b in cells)
    footer = [
        b for b in cells if b["cell"]["start_row_offset_idx"] == last_row and b["text"].strip()
    ]
    if last_row != total_row + 1 or len(footer) != 1:
        return None
    (block,) = footer
    cell, text = block["cell"], block["text"]
    if (
        cell["start_col_offset_idx"] != 0
        or cell["end_col_offset_idx"] != width
        or cell["end_row_offset_idx"] != last_row + 1
        or not re.match(r"\s*total\s*[:：]", text, re.I)
        or re.search(r"\b(?:per|each|or|subtotal|if|approx\w*)\b|每|小计|约", text, re.I)
    ):
        return None
    tokens = list(_VOLUME_TOKEN.finditer(text))
    if len(tokens) != 1:
        return None
    numeric = re.match(r"\d+\.\d+", tokens[0].group())
    result = Decimal(number.group(1).replace(",", "."))
    return (result, block) if numeric and Decimal(numeric.group()) == result else None


def _pdf_table_field(blocks, field_name, *, explicit_total_only=False):
    """Match a uniquely labeled total row to an explicit unit-bearing column."""
    headers = {
        "ctns": r"\b(?:cartons?|ctns?)\b|箱子数量|总箱数",
        "package_count": r"\b(?:packages?|pkgs?)\b",
        "line_net_weight": r"(?<![a-z])(?:n\s*[./]?\s*w|net\s*(?:weight|wt))(?![a-z])",
        "line_gross_weight": r"(?<![a-z])(?:g\s*[./]?\s*w|gross\s*(?:weight|wt))(?![a-z])",
        "line_volume": r"(?:\bcbm\b|m\s*[³3])",
    }
    if field_name not in headers:
        return []
    tables = {}
    for block in blocks:
        match = re.fullmatch(
            r"((?:/parts/\d+/document)?/tables/\d+)/data/table_cells/\d+/text", block["pointer"]
        )
        cell = block.get("cell")
        if not match or not cell:
            continue
        coordinates = [
            cell.get(key)
            for key in (
                "start_row_offset_idx",
                "end_row_offset_idx",
                "start_col_offset_idx",
                "end_col_offset_idx",
            )
        ]
        if any(type(x) is not int or x < 0 for x in coordinates):
            continue
        tables.setdefault(match.group(1), []).append(block)
    found = []
    for cells in tables.values():
        first_row = min(cell["cell"]["start_row_offset_idx"] for cell in cells)
        totals = [
            cell
            for cell in cells
            if re.fullmatch(
                r"\s*(?:(?:grand\s+)?total(?:\s+for\s+\d+\s+items?)?\s*(?:总计|合计)?|(?:总计|合计)(?:\s*total)?)\s*[:：]?\s*",
                cell["text"],
                re.I,
            )
        ]
        marker_headers = []
        if (
            not totals
            and not explicit_total_only
            and (merged := _pdf_total_with_item_quantity(cells))
        ):
            marker, marker_headers = merged
            totals = [marker]
        total_rows = {cell["cell"]["start_row_offset_idx"] for cell in totals}
        if len(total_rows) != 1:
            if not total_rows and not explicit_total_only:
                found.extend(_pdf_unlabeled_carton_total(cells, field_name))
            continue
        total_row = next(iter(total_rows))
        summary_patterns = {
            "line_net_weight": r"(?:^|[^a-z])(?:n\s*[./]?\s*w|net\s*weight)\s*[:：]\s*(\d+(?:\.\d+)?)\s*(kgs?)\b",
            "line_gross_weight": r"(?:^|[^a-z])(?:g\s*[./]?\s*w|gross\s*weight)\s*[:：]\s*(\d+(?:\.\d+)?)\s*(kgs?)\b",
            "line_volume": r"(?:^|[^a-z])(?:cbm|volume)\s*[:：]\s*(\d+(?:\.\d+)?)\s*(cbm|m³|m3)\b",
        }
        summaries = []
        if not explicit_total_only and field_name in summary_patterns:
            for block in cells:
                if block["cell"]["start_row_offset_idx"] == total_row:
                    matches = list(re.finditer(summary_patterns[field_name], block["text"], re.I))
                    if len(matches) == 1:
                        summaries.append((block, matches[0]))
        if len(summaries) == 1:
            block, match = summaries[0]
            value = _decimal_value(match.group(1))
            unit = "CBM" if field_name == "line_volume" else "kg"
            evidence = ([block] if block is totals[0] else [block, totals[0]]) + marker_headers
            found.append((value, unit, evidence))
            if field_name in {"line_net_weight", "line_gross_weight"}:
                weight_headers = [
                    cell
                    for cell in cells
                    if re.search(headers[field_name], cell["text"], re.I)
                    and cell["cell"]["start_row_offset_idx"] < total_row
                    and cell["cell"]["end_col_offset_idx"]
                    == cell["cell"]["start_col_offset_idx"] + 1
                ]
                count_headers = [
                    cell
                    for cell in cells
                    if re.search(r"\b(?:quant|packages?|ctns?)\b|件数", cell["text"], re.I)
                    and cell["cell"]["start_row_offset_idx"] < total_row
                    and cell["cell"]["end_col_offset_idx"]
                    == cell["cell"]["start_col_offset_idx"] + 1
                ]
                data_rows = {
                    cell["cell"]["start_row_offset_idx"]
                    for cell in cells
                    if first_row < cell["cell"]["start_row_offset_idx"] < total_row
                }
                if len(weight_headers) == len(count_headers) == len(data_rows) == 1:
                    row = next(iter(data_rows))
                    weight_col = weight_headers[0]["cell"]["start_col_offset_idx"]
                    count_col = count_headers[0]["cell"]["start_col_offset_idx"]
                    weights = [
                        cell
                        for cell in cells
                        if cell["cell"]["start_row_offset_idx"] == row
                        and cell["cell"]["start_col_offset_idx"] == weight_col
                    ]
                    counts = [
                        cell
                        for cell in cells
                        if cell["cell"]["start_row_offset_idx"] == row
                        and cell["cell"]["start_col_offset_idx"] == count_col
                    ]
                    if len(weights) == len(counts) == 1 and counts[0]["text"].strip() == "1":
                        weight = re.fullmatch(
                            r"\s*(\d+(?:\.\d+)?)\s*kgs?\s*", weights[0]["text"], re.I
                        )
                        if weight and _decimal_value(weight.group(1)) != value:
                            found.append(
                                (
                                    _decimal_value(weight.group(1)),
                                    "kg",
                                    [
                                        weights[0],
                                        weight_headers[0],
                                        counts[0],
                                        count_headers[0],
                                        totals[0],
                                        *marker_headers,
                                    ],
                                )
                            )
            continue
        header_cues = (
            r"\bn\s*[./]?\s*w\b",
            r"\bg\s*[./]?\s*w\b",
            r"\bqty\b|\bquantity\b",
            r"\bcbm\b|m[³3]",
            r"\bpackages?\b|\bcartons?\b",
        )
        inferred_header_rows = {
            row
            for row in range(total_row)
            if sum(
                bool(
                    re.search(
                        cue,
                        " ".join(
                            b["text"] for b in cells if b["cell"]["start_row_offset_idx"] == row
                        ),
                        re.I,
                    )
                )
                for cue in header_cues
            )
            >= 2
        }
        matching_headers = [
            cell
            for cell in cells
            if re.search(headers[field_name], cell["text"], re.I)
            and cell["cell"]["start_row_offset_idx"] < total_row
            and (
                cell["cell"].get("column_header")
                or cell["cell"]["start_row_offset_idx"] == first_row
                or cell["cell"]["start_row_offset_idx"] in inferred_header_rows
            )
            and cell["cell"]["end_col_offset_idx"] == cell["cell"]["start_col_offset_idx"] + 1
            and not (
                field_name == "ctns"
                and re.search(
                    r"\b(?:no\.?|number)\b|(?:\bper\s+|/)\s*(?:cartons?|ctns?)(?![a-z])"
                    r"|\bcartons?\s+(?:size|dimensions?)(?![a-z])",
                    cell["text"],
                    re.I,
                )
            )
        ]
        if field_name in {"line_net_weight", "line_gross_weight"} and len(matching_headers) > 1:
            document_headers = [
                cell
                for cell in matching_headers
                if not re.search(
                    r"\b(?:each|per)\b|/\s*(?:cartons?|ctns?)(?![a-z])|"
                    r"\b(?:net|gross)\s*(?:weight|wt)\s+of\b",
                    cell["text"],
                    re.I,
                )
            ]
            if document_headers:
                matching_headers = document_headers
        if field_name == "ctns":
            volume_headers = {
                (
                    cell["cell"]["start_col_offset_idx"],
                    cell["cell"]["end_col_offset_idx"],
                    cell["cell"]["end_row_offset_idx"],
                )
                for cell in cells
                if re.fullmatch(r"\s*(?:cbm|m\s*[³3])\s*", cell["text"], re.I)
            }
            matching_headers = [
                cell
                for cell in matching_headers
                if not re.search(r"\bcbm\b|m\s*[³3]", cell["text"], re.I)
                and (
                    cell["cell"]["start_col_offset_idx"],
                    cell["cell"]["end_col_offset_idx"],
                    cell["cell"]["start_row_offset_idx"],
                )
                not in volume_headers
            ]
        outer_pack = field_name == "ctns" and not matching_headers
        if outer_pack:
            matching_headers = [
                cell
                for cell in cells
                if re.fullmatch(r"\s*outer\s+pack\s*", cell["text"], re.I)
                and cell["cell"]["start_row_offset_idx"] < total_row
                and cell["cell"]["end_col_offset_idx"] == cell["cell"]["start_col_offset_idx"] + 1
            ]
        total_pattern = (
            r"\btotal\b|总|(?<!\w)t\s*/\s*cbm\b" if field_name == "line_volume" else r"\btotal\b|总"
        )
        total_headers = [
            cell for cell in matching_headers if re.search(total_pattern, cell["text"], re.I)
        ]
        if len(matching_headers) > 1 and len(total_headers) == 1:
            matching_headers = total_headers
        if len(matching_headers) != 1:
            continue
        header = matching_headers[0]
        col = header["cell"]["start_col_offset_idx"]
        values = [
            cell
            for cell in cells
            if cell["cell"]["start_row_offset_idx"] == total_row
            and cell["cell"]["start_col_offset_idx"] == col
            and cell["cell"]["end_col_offset_idx"] == col + 1
        ]
        if len(values) != 1:
            continue
        value_block = values[0]
        match = re.fullmatch(
            r"\s*((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*(kg|kgs|cbm|m³|m3)?\s*",
            value_block["text"],
            re.I,
        )
        carton_matches = list(_COUNT.finditer(value_block["text"])) if field_name == "ctns" else []
        if outer_pack and len(carton_matches) != 1:
            continue
        evidence = [value_block, header, totals[0], *marker_headers]
        paired_volume = (
            _pdf_decimal_volume_footer(cells, total_row, value_block["text"], header["text"])
            if field_name == "line_volume" and not explicit_total_only
            else None
        )
        if paired_volume:
            number, footer = paired_volume
            unit_text = header["text"].lower()
            evidence.append(footer)
        elif match:
            if explicit_total_only and match.group(2):
                suffix = match.group(2).lower()
                allowed_units = (
                    {"cbm", "m³", "m3"}
                    if field_name == "line_volume"
                    else {"kg", "kgs"}
                    if field_name in {"line_net_weight", "line_gross_weight"}
                    else set()
                )
                if suffix not in allowed_units:
                    continue
            number = Decimal(match.group(1).replace(",", ""))
            unit_text = (header["text"] + " " + (match.group(2) or "")).lower()
        elif len(carton_matches) == 1:
            number = Decimal(carton_matches[0].group("number"))
            unit_text = header["text"].lower()
        else:
            continue
        if field_name in {"line_net_weight", "line_gross_weight"} and not re.search(
            r"\bkgs?\b", unit_text
        ):
            unit_headers = [
                cell
                for cell in cells
                if cell["cell"]["start_row_offset_idx"] == header["cell"]["start_row_offset_idx"]
                and re.search(headers[field_name], cell["text"], re.I)
                and re.search(r"\bkgs?\b", cell["text"], re.I)
            ]
            if len(unit_headers) != 1 or header not in total_headers:
                continue
            evidence.append(unit_headers[0])
        if field_name == "line_volume" and not re.search(r"\bcbm\b|m\s*[³3]", unit_text):
            continue
        if field_name in {"ctns", "package_count"} and number != number.to_integral_value():
            continue
        unit = (
            None
            if field_name in {"ctns", "package_count"}
            else "CBM"
            if field_name == "line_volume"
            else "kg"
        )
        found.append((_decimal_value(number), unit, evidence))
    return found


_PDF_ROW_HEADERS = {
    "ctns": ("total", "ctn"),
    "line_net_weight": ("total", "n.w(kg)"),
    "line_gross_weight": ("total", "g.w(kg)"),
    "unit_net_weight": ("n.weight", "(kg)/ctn"),
    "unit_gross_weight": ("g.weight", "(kg)/ctn"),
    "line_volume": ("total", "cbm"),
}
_PDF_AUXILIARY_HEADERS = {"quantity_per_carton": ("qty/", "ctn")}


def _pdf_row_headers(entries, names, row_box):
    upper_name, lower_name = names

    def label(block):
        return re.sub(r"\s+", "", block["text"]).casefold()

    return [
        (upper, lower, lower_box)
        for upper, upper_box in entries
        if label(upper) == upper_name
        for lower, lower_box in entries
        if label(lower) == lower_name
        and _overlaps_x(upper_box, lower_box)
        and 0 <= upper_box["b"] - lower_box["t"] <= 2 * (upper_box["t"] - upper_box["b"])
        and lower_box["b"] > row_box["t"]
        and row_box["r"] < lower_box["l"]
    ]


def pdf_row_word_supplement_page(execution, requirements, unresolved_fields):
    """Find a uniquely selected row with an unresolved, visibly merged numeric cell."""
    if (
        not execution
        or not execution.get("ref")
        or execution["executed_profile"]["reader"] not in PDF_READERS
        or execution["executed_profile"]["options"].get("text_cell_unit") != "line"
    ):
        return None
    anchors = {item.get("pdf_row_anchor") for item in requirements if item.get("pdf_row_anchor")}
    if len(anchors) != 1:
        return None
    anchor = next(iter(anchors))
    pattern = re.compile(r"(?<![A-Za-z0-9])" + re.escape(anchor) + r"(?![A-Za-z0-9])", re.I)
    pages = _positioned_pdf_blocks(_blocks(execution)[1])
    rows = [
        (page, box)
        for page, entries in pages.items()
        for block, box in entries
        if pattern.search(block["text"])
    ]
    if len(rows) != 1:
        return None
    page, row_box = rows[0]
    entries = pages[page]
    if "product_name" in unresolved_fields and any(
        item.get("pdf_cargo") and item["field_name"] == "product_name" for item in requirements
    ):
        # A line block crossing the Description column cannot prove its cell text.
        if any(
            block["text"].strip().casefold() in {"description", "品名"}
            and box["b"] > row_box["t"]
            and _overlaps_x(box, row_box)
            for block, box in entries
        ):
            return page
    header_names = {**_PDF_ROW_HEADERS, **_PDF_AUXILIARY_HEADERS}
    headers = {
        name: _pdf_row_headers(entries, labels, row_box) for name, labels in header_names.items()
    }
    for field in unresolved_fields:
        target_headers = headers.get(field, [])
        if len(target_headers) != 1:
            continue
        target_box = target_headers[0][2]
        other_boxes = [
            matches[0][2]
            for name, matches in headers.items()
            if name != field and len(matches) == 1
        ]
        for block, box in entries:
            if (
                re.fullmatch(r"\s*\d+(?:\.\d+)?(?:\s+\d+(?:\.\d+)?)+\s*", block["text"])
                and _overlaps_y(box, row_box)
                and _overlaps_x(box, target_box)
                and any(_overlaps_x(box, other) for other in other_boxes)
            ):
                return page
    return None


def _pdf_row_field(blocks, field_name, anchor):
    """Resolve a numeric PDF row value from one row marker and its column headers."""
    pages = _positioned_pdf_blocks(blocks)
    anchor_pattern = re.compile(r"(?<![A-Za-z0-9])" + re.escape(anchor) + r"(?![A-Za-z0-9])", re.I)
    anchors = [
        (page, block, box)
        for page, entries in pages.items()
        for block, box in entries
        if anchor_pattern.search(block["text"])
    ]
    if len(anchors) != 1:
        return None
    page, row, row_box = anchors[0]
    entries = pages[page]

    def headers_for(name):
        return _pdf_row_headers(entries, _PDF_ROW_HEADERS[name], row_box)

    headers = headers_for(field_name)
    if len(headers) != 1:
        return None
    upper, lower, header_box = headers[0]
    values = [
        (block, box)
        for block, box in entries
        if re.fullmatch(r"\d+(?:\.\d+)?", block["text"].strip())
        and header_box["l"] <= box["l"] < box["r"] <= header_box["r"]
        and _overlaps_y(box, row_box)
    ]
    if len(values) != 1:
        return None
    value_block, value_box = values[0]
    if any(
        _overlaps_x(value_box, other_box)
        for other_name, labels in {**_PDF_ROW_HEADERS, **_PDF_AUXILIARY_HEADERS}.items()
        if other_name != field_name
        for _, _, other_box in _pdf_row_headers(entries, labels, row_box)
    ):
        return None
    value = _decimal_value(value_block["text"].strip())
    if field_name == "ctns" and not isinstance(value, int):
        return None
    return value, [value_block, upper, lower, row]


_PACKAGE_EXPRESSION = re.compile(
    r"\s*(?:bulto\s*:\s*)?(\d+)\s*(wooden\s+(?:box(?:es)?|cases?|crates?)|cartons?|ctns?|pallets?|packages?)\s*\.?\s*",
    re.I,
)
_DIMENSIONS_EXPRESSION = re.compile(
    r"\s*(\d+(?:\.\d+)?)\s*[x×*]\s*(\d+(?:\.\d+)?)\s*[x×*]\s*"
    r"(\d+(?:\.\d+)?)\s*(cm)\s*",
    re.I,
)


def _package_expression(text):
    """Return the count and explicitly stated kind; 'package' has no specific kind."""
    match = _PACKAGE_EXPRESSION.fullmatch(text)
    if not match:
        return None
    label = match.group(2).lower()
    kind = (
        "carton"
        if label.startswith(("carton", "ctn"))
        else (
            "pallet"
            if label.startswith("pallet")
            else "wooden_case"
            if label.startswith("wooden")
            else None
        )
    )
    return int(match.group(1)), kind


def _pdf_adjacent_value(blocks, label_pattern, value_pattern):
    """Find one uniquely labelled value in the same PDF row, with no intervening text."""
    pages = _positioned_pdf_blocks(blocks)
    labels = [
        (page, block, box)
        for page, entries in pages.items()
        for block, box in entries
        if label_pattern.fullmatch(block["text"].strip())
    ]
    if len(labels) != 1:
        return None
    page, label, label_box = labels[0]
    pair = _pdf_value_to_right(pages[page], label, label_box, value_pattern)
    return (page, pair[0], label) if pair else None


def _pdf_value_to_right(entries, label, label_box, value_pattern):
    """Require one value and no intervening text on the label's visual row."""

    def same_row(box):
        overlap = min(label_box["t"], box["t"]) - max(label_box["b"], box["b"])
        return overlap >= 0.5 * min(label_box["t"] - label_box["b"], box["t"] - box["b"])

    matches = [
        (block, box)
        for block, box in entries
        if box["l"] >= label_box["r"] and same_row(box) and value_pattern.fullmatch(block["text"])
    ]
    if len(matches) != 1:
        return None
    value, value_box = matches[0]
    if any(
        block["pointer"] not in {label["pointer"], value["pointer"]}
        and block["text"].strip()
        and same_row(box)
        and box["r"] > label_box["r"]
        and box["l"] < value_box["l"]
        for block, box in entries
    ):
        return None
    return value, value_box


_PDF_SUMMARY_WEIGHT = r"(?:\d{1,3}(?:,\d{3})+\.\d+|\d+(?:\.\d+)?)(?:\s*kgs?)?"
_PDF_SUMMARY_ROWS = {
    "package_count": (r"qty\.?\s+of\s+volumes\s*[:：]", r"[1-9]\d*"),
    "package_type": (r"package\s+type\s*[:：]", r"(?:cartons?|ctns?|pallets?)"),
    "line_net_weight": (
        r"(?:total\s+)?net\s+weight(?:\s*\(\s*kgs?\s*\))?\s*[:：]",
        _PDF_SUMMARY_WEIGHT,
    ),
    "line_gross_weight": (
        r"(?:total\s+)?gross\s+weight(?:\s*\(\s*kgs?\s*\))?\s*[:：]",
        _PDF_SUMMARY_WEIGHT,
    ),
}
_PDF_SUMMARY_PATTERNS = {
    name: (
        re.compile(label, re.I),
        re.compile(rf"\s*(?:{value})\s*", re.I),
        re.compile(rf"(?P<label>{label})\s*(?P<value>{value})", re.I),
    )
    for name, (label, value) in _PDF_SUMMARY_ROWS.items()
}


def _pdf_packing_summary(blocks):
    """Read a same-page packing summary anchored by package quantity and type."""
    result = {}
    for entries in _positioned_pdf_blocks(blocks).values():
        passes = {}
        for block, box in entries:
            passes.setdefault(block["pointer"].rsplit("/texts/", 1)[0], []).append((block, box))
        for entries in passes.values():
            rows = {name: [] for name in _PDF_SUMMARY_ROWS}
            for block, box in entries:
                text = block["text"].strip()
                for name, (label, value_pattern, inline_pattern) in _PDF_SUMMARY_PATTERNS.items():
                    inline = inline_pattern.fullmatch(text)
                    if inline:
                        label_text, raw = inline["label"], inline["value"].strip()
                        evidence, anchor = [block], box
                    elif label.fullmatch(text) and (
                        pair := _pdf_value_to_right(entries, block, box, value_pattern)
                    ):
                        value_block, anchor = pair
                        label_text, raw = text, value_block["text"].strip()
                        evidence = [value_block, block]
                    else:
                        continue
                    if name == "package_count":
                        value = int(raw)
                    elif name == "package_type":
                        value = _package_expression("1 " + raw)[1]
                    else:
                        if not re.search(r"\bkgs?\b", label_text, re.I) and not re.search(
                            r"kgs?$", raw, re.I
                        ):
                            continue
                        value = _decimal_value(
                            re.sub(r"\s*kgs?$", "", raw, flags=re.I).replace(",", "")
                        )
                    rows[name].append((value, evidence, anchor))

            def aligned(left, right):
                return abs(left["r"] - right["r"]) <= min(
                    left["t"] - left["b"], right["t"] - right["b"]
                )

            def near(left, right, line_limit):
                center_diff = abs((left["t"] + left["b"]) - (right["t"] + right["b"])) / 2
                return aligned(left, right) and center_diff <= line_limit * max(
                    left["t"] - left["b"], right["t"] - right["b"]
                )

            groups = []
            for count in rows["package_count"]:
                types = [kind for kind in rows["package_type"] if near(count[2], kind[2], 6)]
                if len(types) == 1:
                    groups.append((count, types[0]))
            groups = [
                (count, kind)
                for count, kind in groups
                if sum(other_kind is kind for _, other_kind in groups) == 1
            ]
            for count, kind in groups:
                result.setdefault("package_count", []).append((count[0], None, count[1] + kind[1]))
                result.setdefault("package_type", []).append((kind[0], None, kind[1] + count[1]))
                if kind[0] == "carton":
                    result.setdefault("ctns", []).append((count[0], None, count[1] + kind[1]))
            for name in ("line_net_weight", "line_gross_weight"):
                for value, evidence, box in rows[name]:
                    anchored = [
                        (count, kind)
                        for count, kind in groups
                        if aligned(box, count[2])
                        and aligned(box, kind[2])
                        and (near(box, count[2], 4) or near(box, kind[2], 4))
                    ]
                    if len(anchored) == 1:
                        count, kind = anchored[0]
                        result.setdefault(name, []).append(
                            (value, "kg", evidence + count[1] + kind[1])
                        )
    if {value for value, _, _ in result.get("package_type", [])} != {"carton"}:
        result.pop("ctns", None)
    return result


def _pdf_document_package(blocks):
    match = _pdf_adjacent_value(
        blocks,
        re.compile(r"total\s+packages\s*/\s*type\s*[:：]?", re.I),
        _PACKAGE_EXPRESSION,
    )
    adjacent = None
    if match is not None:
        page, value, label = match
        count, kind = _package_expression(value["text"])
        adjacent = (page, count, kind, [value, label])
    packed = []
    for page, entries in _positioned_pdf_blocks(blocks).items():
        passes = {}
        for block, _ in entries:
            stated = re.fullmatch(
                r"\s*packed\s+in\s+(one|\d+)\s+(cartons?|ctns?|pallets?|packages?)\s+only\.?\s*",
                block["text"],
                re.I,
            )
            if stated:
                count = "1" if stated.group(1).lower() == "one" else stated.group(1)
                count, kind = _package_expression(count + " " + stated.group(2))
                packed.append((page, count, kind, [block]))
        for block, box in entries:
            passes.setdefault(block["pointer"].rsplit("/texts/", 1)[0], []).append((block, box))
        for page_entries in passes.values():
            headers = [
                (b, box)
                for b, box in page_entries
                if re.fullmatch(r"wooden\s+case", b["text"].strip(), re.I)
            ]
            rows = [
                (b, box)
                for b, box in page_entries
                if re.fullmatch(r"total\s*[:：]?", b["text"].strip(), re.I)
            ]
            if len(headers) != 1 or len(rows) != 1:
                continue
            header, header_box = headers[0]
            row, row_box = rows[0]
            values = [
                b
                for b, box in page_entries
                if b["text"].strip().isdigit()
                and _overlaps_x(box, header_box)
                and _overlaps_y(box, row_box)
            ]
            if len(values) == 1:
                packed.append(
                    (page, int(values[0]["text"].strip()), "wooden_case", [values[0], header, row])
                )
    identities = {item[:3] for item in packed}
    if adjacent:
        identities.add(adjacent[:3])
    if len(identities) != 1:
        return None
    return adjacent or packed[0]


def _pdf_document_raw_dimensions(blocks):
    return _pdf_adjacent_value(
        blocks,
        re.compile(r"outer\s+dimensions(?:\s*\(\s*l\s*x\s*w\s*x\s*h\s*\))?\s*[:：]?", re.I),
        _DIMENSIONS_EXPRESSION,
    )


def _pdf_document_dimensions(blocks):
    package = _pdf_document_package(blocks)
    match = _pdf_document_raw_dimensions(blocks)
    if not package or not match or package[0] != match[0] or package[1:3] != (1, "carton"):
        return None
    _, value, label = match
    if not re.fullmatch(
        r"outer\s+dimensions\s*\(\s*l\s*x\s*w\s*x\s*h\s*\)\s*[:：]?",
        label["text"].strip(),
        re.I,
    ):
        return None
    parts = _DIMENSIONS_EXPRESSION.fullmatch(value["text"])
    return tuple(_decimal_value(parts.group(i)) for i in (1, 2, 3)), [value, label, *package[3]]


def _pdf_cargo_row(blocks, anchor):
    pattern = re.compile(r"(?<![A-Za-z0-9])" + re.escape(anchor) + r"(?![A-Za-z0-9])", re.I)
    pages = _positioned_pdf_blocks(blocks)
    matches = [
        (page, block, box)
        for page, entries in pages.items()
        for block, box in entries
        if pattern.search(block["text"])
    ]
    if len(matches) != 1:
        return None
    page, row, box = matches[0]
    return pages[page], row, box


def _pdf_row_product(blocks, anchor):
    selected = _pdf_cargo_row(blocks, anchor)
    if not selected:
        return None
    entries, row, row_box = selected
    descriptions = [
        (block, box)
        for block, box in entries
        if block["text"].strip().casefold() in {"description", "品名"} and box["b"] > row_box["t"]
    ]
    if len(descriptions) != 1:
        return None
    description, desc_box = descriptions[0]
    left = []
    for block, box in entries:
        label = block["text"].strip().casefold()
        if (
            label not in {"code", "hs code"}
            or not _overlaps_y(box, desc_box)
            or box["r"] >= desc_box["l"]
        ):
            continue
        preceding = [
            (other, other_box)
            for other, other_box in entries
            if other["text"].strip().casefold() == "hs"
            and _overlaps_y(other_box, box)
            and other_box["r"] <= box["l"]
        ]
        if label == "hs code":
            left.append(([block], box["l"], box["r"]))
        elif len(preceding) == 1:
            left.append(([preceding[0][0], block], preceding[0][1]["l"], box["r"]))
    right = _pdf_row_headers(entries, ("total", "qty(pcs)"), row_box)
    if len(left) != 1 or len(right) != 1 or desc_box["r"] >= right[0][2]["l"]:
        return None
    left_blocks, left_edge, left_right = left[0]
    upper, lower, right_box = right[0]
    left_boundary = ((left_edge + left_right) / 2 + (desc_box["l"] + desc_box["r"]) / 2) / 2
    # A line-level block crossing a boundary cannot be cut into a trusted cell.
    if any(
        _overlaps_y(box, row_box)
        and block["text"].strip()
        and ((box["l"] < left_boundary < box["r"]) or (box["l"] < right_box["l"] < box["r"]))
        for block, box in entries
    ):
        return None
    fragments = sorted(
        [
            (block, box)
            for block, box in entries
            if _overlaps_y(box, row_box)
            and left_boundary <= box["l"] < box["r"] <= right_box["l"]
            and block["text"].strip()
        ],
        key=lambda item: item[1]["l"],
    )
    if not fragments:
        return None
    values = [item[0] for item in fragments]
    return (
        " ".join(block["text"].strip() for block in values),
        values,
        [*values, description, *left_blocks, upper, lower, row],
    )


def _pdf_row_pallet_measurement(blocks, anchor):
    selected = _pdf_cargo_row(blocks, anchor)
    if not selected:
        return None
    entries, row, row_box = selected
    headers = _pdf_row_headers(entries, ("measurement", "(cm)/pallets"), row_box)
    if len(headers) != 1:
        return None
    upper, lower, header_box = headers[0]
    values = [
        block
        for block, box in entries
        if _overlaps_y(box, row_box)
        and header_box["l"] <= box["l"] < box["r"] <= header_box["r"]
        and re.fullmatch(
            r"\s*\d+(?:\.\d+)?\s*[x×*]\s*\d+(?:\.\d+)?\s*[x×*]\s*\d+(?:\.\d+)?\s*",
            block["text"],
            re.I,
        )
    ]
    if len(values) != 1:
        return None
    return values[0]["text"], [values[0]], [values[0], upper, lower, row]


def _pdf_cargo_field(blocks, requirement):
    """Return value, unit, raw value blocks and all supporting blocks."""
    name, anchor = requirement["field_name"], requirement.get("pdf_row_anchor")
    if anchor:
        if name == "product_name":
            result = _pdf_row_product(blocks, anchor)
            return (result[0], None, result[1], result[2]) if result else None
        if name == "raw_observations":
            result = _pdf_row_pallet_measurement(blocks, anchor)
            return (result[0], None, result[1], result[2]) if result else None
        if name in {"package_count", "package_type"}:
            result = _pdf_row_field(blocks, "ctns", anchor)
            if result:
                count, evidence = result
                return (
                    count if name == "package_count" else "carton",
                    None,
                    evidence[:1],
                    evidence,
                )
        return None
    package = _pdf_document_package(blocks)
    if name in {"ctns", "package_count", "package_type"} and package:
        _, count, kind, evidence = package
        value = (
            count
            if name == "package_count" or (name == "ctns" and kind == "carton")
            else (kind if name == "package_type" else None)
        )
        if value is not None:
            return value, None, evidence[:1], evidence
    if name == "raw_observations":
        raw = _pdf_document_raw_dimensions(blocks)
        if raw:
            _, value, label = raw
            confirmed = _pdf_document_dimensions(blocks)
            evidence = confirmed[1] if confirmed else [value, label]
            return value["text"], None, [value], evidence
    if name in {"length_cm", "width_cm", "height_cm"}:
        dimensions = _pdf_document_dimensions(blocks)
        if dimensions:
            values, evidence = dimensions
            return (
                values[{"length_cm": 0, "width_cm": 1, "height_cm": 2}[name]],
                "cm",
                evidence[:1],
                evidence,
            )
    return None


def _pdf_cargo_field_supported(candidate, sources, bindings, executions, profile):
    requirements = [
        item
        for item in profile["requirements"]
        if item.get("pdf_cargo")
        and item["field_name"] == candidate["field_name"]
        and item["object_selector"] == candidate["object_id"]
    ]
    field = candidate["field"]
    ids = field["source_ids"]
    if len(requirements) != 1 or not ids:
        return False
    artifact_ids = {bindings[sid]["artifact_id"] for sid in ids}
    if len(artifact_ids) != 1:
        return False
    execution = executions.get(next(iter(artifact_ids)))
    if not execution or execution["executed_profile"]["reader"] not in PDF_READERS:
        return False
    for value, unit, raw_blocks, evidence_blocks in _pdf_cargo_candidates(
        _blocks(execution)[1], requirements[0]
    ):
        if (
            value != field["value"]
            or unit != field.get("unit")
            or field["raw_values"] != [{"value": block["text"]} for block in raw_blocks]
        ):
            continue
        expected = {block["pointer"]: block for block in evidence_blocks}
        if len(expected) != len(evidence_blocks) or len(expected) != len(ids):
            continue
        for sid in ids:
            source, binding = sources[sid], bindings[sid]
            loc = source["locator"]
            block = expected.pop(loc["json_pointer"], None)
            if (
                block is None
                or source["kind"] != "attachment"
                or source["excerpt"] != block["text"]
                or loc["char_start"] != 0
                or loc["char_end"] != len(block["text"])
                or loc.get("page") != block["provenance"][0]["page_no"]
                or binding["docling_provenance"] != block["provenance"]
            ):
                break
        else:
            return not expected
    return False


def _pdf_cargo_candidates(blocks, requirement):
    if existing := _pdf_cargo_field(blocks, requirement):
        yield existing
    name = requirement["field_name"]
    if not requirement.get("pdf_row_anchor") and name == "package_count":
        for value, unit, evidence in _pdf_table_field(blocks, name):
            yield value, unit, evidence[:1], evidence
    if not requirement.get("pdf_row_anchor") and name in {"ctns", "package_count", "package_type"}:
        for value, unit, evidence in _pdf_packing_summary(blocks).get(name, []):
            yield value, unit, evidence[:1], evidence


def _pdf_inline_mixed_total(blocks, field_name):
    if field_name not in {"ctns", "line_volume"}:
        return
    for block in blocks:
        text = block["text"]
        if "/texts/" not in block["pointer"] or not re.match(r"\s*total\s*[:：]", text, re.I):
            continue
        cartons = list(_COUNT.finditer(text))
        volumes = list(_VOLUME_TOKEN.finditer(text))
        if len(cartons) != 1 or len(volumes) != 1 or re.search(r"\b(?:each|per)\b", text, re.I):
            continue
        if field_name == "ctns":
            number = Decimal(cartons[0].group("number"))
            if number == number.to_integral_value():
                yield _decimal_value(number), None, [block]
        else:
            yield (
                _decimal_value(re.match(r"\d+(?:\.\d+)?", volumes[0].group()).group()),
                "CBM",
                [block],
            )


def _pdf_gross_weight_column(blocks):
    """Read a gross-weight value below its two-line PDF column header."""
    for entries in _positioned_pdf_blocks(blocks).values():
        for gross, gross_box in entries:
            if gross["text"].strip().casefold() != "gross":
                continue
            part = gross["pointer"].split("/texts/")[0]
            weights = [
                (b, box)
                for b, box in entries
                if b["pointer"].split("/texts/")[0] == part
                and b["text"].strip().casefold() == "weight"
                and _overlaps_x(box, gross_box)
                and 0 <= gross_box["b"] - box["t"] <= 2 * (gross_box["t"] - gross_box["b"])
            ]
            if len(weights) != 1:
                continue
            weight, weight_box = weights[0]
            values = [
                (b, re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*kgs?\s*", b["text"], re.I))
                for b, box in entries
                if b["pointer"].split("/texts/")[0] == part
                and _overlaps_x(box, weight_box)
                and 0 <= weight_box["b"] - box["t"] <= 3 * (weight_box["t"] - weight_box["b"])
            ]
            values = [(b, match) for b, match in values if match]
            if len(values) == 1:
                value, match = values[0]
                yield _decimal_value(match.group(1)), "kg", [value, gross, weight]


def _pdf_document_total_candidates(blocks, field_name):
    yield from _pdf_table_field(blocks, field_name)
    yield from _pdf_inline_mixed_total(blocks, field_name)
    if field_name == "line_gross_weight":
        yield from _pdf_gross_weight_column(blocks)
    if field_name == "line_volume" and (positioned := _pdf_total_volume(blocks)):
        yield positioned[0], "CBM", positioned[1]
    if field_name in {"line_gross_weight", "line_volume"}:
        for block in blocks:
            text = block["text"]
            if "/texts/" not in block["pointer"] or not re.match(
                r"\s*(?:comm(?:odity)?|cargo(?:\s+details)?)\s*[:：]", text, re.I
            ):
                continue
            gross = list(
                re.finditer(r"\bgross\s*weight\s*[-:：]?\s*(\d+(?:\.\d+)?)\s*kgs?\b", text, re.I)
            )
            volumes = list(_VOLUME_TOKEN.finditer(text))
            if len(gross) != 1 or len(volumes) != 1 or re.search(r"\b(?:each|per)\b", text, re.I):
                continue
            if field_name == "line_gross_weight":
                yield _decimal_value(gross[0].group(1)), "kg", [block]
            else:
                yield (
                    _decimal_value(re.match(r"\d+(?:\.\d+)?", volumes[0].group()).group()),
                    "CBM",
                    [block],
                )
    if field_name in {"line_net_weight", "line_gross_weight"}:
        yield from _pdf_packing_summary(blocks).get(field_name, [])


_PACKING_COLUMN_LABELS = {
    "sequence": (r"seq\.?", None),
    "ctns": (r"ctns?\s*/?", "箱数"),
    "line_net_weight": (r"total\s+n\.?\s*w\.?\s*/?\s*(?:\(kgs?\))?", "总净重"),
    "line_gross_weight": (r"total\s+g\.?\s*w\.?\s*/?\s*(?:\(kgs?\))?", "总毛重"),
    "line_volume": (r"cbm", "(体积)"),
}
_PACKING_NUMBER = re.compile(r"\s*(\d+(?:\.\d+)?)\s*(kgs?)?\s*", re.I)


def _pdf_packing_total_columns(blocks):
    """Bind the unlabelled final row only when all packing columns reconcile."""
    for page in _positioned_pdf_blocks(blocks).values():
        parts = {}
        for block, box in page:
            parts.setdefault(block["pointer"].split("/texts/")[0], []).append((block, box))
        for entries in parts.values():
            headers = {
                name: [
                    (b, box) for b, box in entries if re.fullmatch(pattern, b["text"].strip(), re.I)
                ]
                for name, (pattern, _) in _PACKING_COLUMN_LABELS.items()
            }
            if any(len(matches) != 1 for matches in headers.values()):
                continue
            heads = {name: matches[0] for name, matches in headers.items()}
            ordered = sorted(heads.values(), key=lambda item: item[1]["l"])
            if max(box["b"] for _, box in heads.values()) >= min(
                box["t"] for _, box in heads.values()
            ) or any(
                _overlaps_x(left, right)
                for (_, left), (_, right) in zip(ordered, ordered[1:], strict=False)
            ):
                continue
            columns = {
                name: [
                    (b, box, match)
                    for b, box in entries
                    if box["t"] < header_box["b"]
                    and _overlaps_x(box, header_box)
                    and (match := _PACKING_NUMBER.fullmatch(b["text"]))
                ]
                for name, (_, header_box) in heads.items()
            }
            cartons = sorted(columns["ctns"], key=lambda item: -item[1]["t"])
            if len(cartons) < 3 or any(
                _overlaps_y(a[1], b[1]) for a, b in zip(cartons, cartons[1:], strict=False)
            ):
                continue
            rows = [
                {
                    name: [item for item in cells if _overlaps_y(item[1], carton_box)]
                    for name, cells in columns.items()
                }
                for _, carton_box, _ in cartons
            ]
            totals_names = set(columns) - {"sequence"}
            if any(len(row[name]) != 1 for row in rows for name in totals_names) or any(
                len({row[name][0][0]["pointer"] for name in totals_names}) != len(totals_names)
                for row in rows
            ):
                continue
            if (
                rows[-1]["sequence"]
                or len(columns["sequence"]) != len(rows) - 1
                or any(
                    len(row["sequence"]) != 1 or row["sequence"][0][2].group(1) != str(i)
                    for i, row in enumerate(rows[:-1], 1)
                )
            ):
                continue
            proofs = {}
            for name, (header, header_box) in heads.items():
                if name == "sequence":
                    continue
                cells = [row[name][0] for row in rows]
                if name in {"ctns", "line_volume"} and any(cell[2].group(2) for cell in cells):
                    break
                numbers = [Decimal(cell[2].group(1)) for cell in cells]
                total = numbers[-1]
                tolerance = (
                    sum(
                        Decimal(1).scaleb(number.as_tuple().exponent) / 2 for number in numbers[:-1]
                    )
                    if name == "line_volume"
                    else Decimal(0)
                )
                if abs(sum(numbers[:-1]) - total) > tolerance or (
                    name == "ctns" and any(n != n.to_integral_value() for n in numbers)
                ):
                    break
                label = _PACKING_COLUMN_LABELS[name][1]
                translated = [
                    b
                    for b, box in entries
                    if b["text"].strip() == label
                    and _overlaps_x(box, header_box)
                    and 0 <= header_box["b"] - box["t"] <= 2 * (header_box["t"] - header_box["b"])
                ]
                if len(translated) > 1:
                    break
                value = cells[-1][0]
                unit = (
                    "CBM"
                    if name == "line_volume"
                    else "kg"
                    if name != "ctns"
                    and (cells[-1][2].group(2) or re.search(r"\bkgs?\b", header["text"], re.I))
                    else None
                )
                proofs[name] = (_decimal_value(total), unit, [value, header, *translated])
            else:
                yield proofs


_OCR_FOOTER_NUMBER = r"(?P<number>\d+(?:,\d+)?)"
_OCR_FOOTER_COLUMNS = {
    "package_count": (r"bultos\s+packages", r"(?P<number>[1-9]\d*)", None),
    "line_gross_weight": (
        r"peso\s+bruto\s+(?:p\.b\.\s+)?kg\.?",
        rf"gross\s+weight\s+(?:g\.w\.\s+)?{_OCR_FOOTER_NUMBER}",
        "kg",
    ),
    "line_net_weight": (
        rf"peso\s+neto\s+(?:p\.[bn]\.\s+)?kg\.?\s+"
        rf"net\s+weight\s+(?:[ng]\.w\.\s+)?{_OCR_FOOTER_NUMBER}",
        None,
        "kg",
    ),
    "line_volume": (r"volumen\s+m[3³]\.?\s+volume", _OCR_FOOTER_NUMBER, "CBM"),
}


def _pdf_ocr_packing_footer(blocks, *, scanned=False):
    """Prove the four totals together in one bilingual packing-list footer."""

    def matches(entries, pattern):
        return [
            (block, box, match)
            for block, box in entries
            if (match := re.fullmatch(pattern, block["text"].strip(), re.I))
        ]

    for page in _positioned_pdf_blocks(blocks).values():
        parts = {}
        for block, box in page:
            if block.get("pdf_pass") == "ocr":
                parts.setdefault(block["pointer"].split("/texts/")[0], []).append((block, box))
        for entries in parts.values():
            markers = matches(entries, r"total\s+s\s*u\s*m\s*a\s*[:.]?")
            if len(markers) != 1:
                continue
            context = []
            if scanned:
                titles = [(b, box) for b, box in entries if pdf_page_title(b["text"]) == "packing"]
                if (
                    len(titles) != 1
                    or titles[0][1]["b"] <= markers[0][1]["t"]
                    or any(pdf_page_title(b["text"]) == "invoice" for b, _ in entries)
                ):
                    continue
                context = [markers[0][0], titles[0][0]]
            headers, values, proofs = [], [], {}
            for name, (label, value_pattern, unit) in _OCR_FOOTER_COLUMNS.items():
                if scanned:
                    label = label.replace(_OCR_FOOTER_NUMBER, r"(?P<number>\d+(?:,[ \t]?\d+)?)")
                    if value_pattern:
                        value_pattern = value_pattern.replace(
                            "gross\\s+weight", "gross\\s*weight"
                        ).replace(_OCR_FOOTER_NUMBER, r"(?P<number>\d+(?:,[ \t]?\d+)?)")
                labels = matches(entries, label)
                if len(labels) != 1:
                    break
                header, box, match = labels[0]
                headers.append(box)
                value, value_box, evidence = header, box, [header]
                if value_pattern:
                    height = box["t"] - box["b"]
                    below = [
                        (b, vbox, m)
                        for b, vbox, m in matches(entries, value_pattern)
                        if box["l"] < (vbox["l"] + vbox["r"]) / 2 < box["r"]
                        and vbox["b"] < box["b"]
                        and (-0.1 * height if scanned else 0) <= box["b"] - vbox["t"] <= height
                    ]
                    if len(below) != 1:
                        break
                    value, value_box, match = below[0]
                    evidence = [value, header]
                values.append(value_box)
                number = re.sub(r",[ \t]?", ".", match["number"])
                proofs[name] = (_decimal_value(number), unit, evidence + context)
            else:
                row_height = min(b["t"] - b["b"] for b in values)
                header_top = max(b["t"] for b in headers)
                if (
                    header_top - min(b["t"] for b in headers) <= row_height
                    and max(b["b"] for b in values) - min(b["b"] for b in values) <= row_height
                    and 0 <= markers[0][1]["b"] - header_top <= 3 * row_height
                    and all(
                        left["r"] <= right["l"]
                        for left, right in zip(headers, headers[1:], strict=False)
                    )
                ):
                    yield proofs


def _attachment_document_totals(blocks, requirement, reader):
    name = requirement["field_name"]
    if (
        requirement["source_kind"] != "attachment"
        or requirement["object_selector"] != requirement["material_id"] + "/document"
        or any(requirement.get(k) for k in ("object_anchor", "pdf_row_anchor", "source_pattern"))
    ):
        return
    if (
        reader in {"DoclingPdfAdaptive", PDF_SCANNED}
        and name in _OCR_FOOTER_COLUMNS
        and (
            requirement.get("pdf_table_total")
            or (name == "package_count" and requirement.get("pdf_cargo"))
        )
    ):
        for footer in _pdf_ocr_packing_footer(blocks, scanned=reader == PDF_SCANNED):
            yield footer[name]
    if reader == "DoclingExcel" and name in _PACKING_COLUMN_LABELS and name != "sequence":
        yield from _pdf_table_field(blocks, name, explicit_total_only=True)
    elif requirement.get("pdf_table_total"):
        if reader == "DoclingPdfAdaptive" or reader in PDF_STRUCTURED_READERS:
            yield from _pdf_document_total_candidates(blocks, name)
        if reader in PDF_READERS and name in _PACKING_COLUMN_LABELS and name != "sequence":
            for proof in _pdf_packing_total_columns(blocks):
                value, unit, evidence = proof[name]
                if unit is not None or name == "ctns":
                    yield value, unit, evidence


def _pdf_row_field_supported(candidate, value, sources, bindings, executions, profile):
    field = candidate["field"]
    requirements = [
        item
        for item in profile["requirements"]
        if item.get("pdf_row_anchor")
        and item["field_name"] == candidate["field_name"]
        and item["object_selector"] == candidate["object_id"]
    ]
    if len(requirements) != 1 or len(field["source_ids"]) != 4:
        return False
    expected_unit = (
        None
        if candidate["field_name"] == "ctns"
        else ("CBM" if candidate["field_name"] == "line_volume" else "kg")
    )
    if field.get("unit") != expected_unit:
        return False
    ids = field["source_ids"]
    artifact_ids = {bindings[sid]["artifact_id"] for sid in ids}
    if len(artifact_ids) != 1:
        return False
    execution = executions.get(next(iter(artifact_ids)))
    if not execution or execution["executed_profile"]["reader"] not in PDF_READERS:
        return False
    found = _pdf_row_field(
        _blocks(execution)[1], candidate["field_name"], requirements[0]["pdf_row_anchor"]
    )
    if found is None or found[0] != value:
        return False
    expected_blocks = {block["pointer"]: block for block in found[1]}
    if len(expected_blocks) != 4:
        return False
    for sid in ids:
        source, binding = sources[sid], bindings[sid]
        loc = source["locator"]
        block = expected_blocks.pop(loc["json_pointer"], None)
        if (
            block is None
            or source["kind"] != "attachment"
            or source["excerpt"] != block["text"]
            or loc["char_start"] != 0
            or loc["char_end"] != len(block["text"])
            or loc.get("page") != block["provenance"][0]["page_no"]
            or binding["docling_provenance"] != block["provenance"]
        ):
            return False
    return not expected_blocks and field["raw_values"] == [{"value": found[1][0]["text"]}]


def _pdf_total_volume_supported(candidate, value, sources, bindings, executions, profile):
    field = candidate["field"]
    if (
        candidate["field_name"] != "line_volume"
        or field.get("unit") != "CBM"
        or not any(
            item.get("pdf_total_volume") is True
            and item["field_name"] == candidate["field_name"]
            and item["object_selector"] == candidate["object_id"]
            for item in profile["requirements"]
        )
        or len(field["source_ids"]) != 4
    ):
        return False
    ids = field["source_ids"]
    artifact_ids = {bindings[sid]["artifact_id"] for sid in ids}
    if len(artifact_ids) != 1:
        return False
    execution = executions.get(next(iter(artifact_ids)))
    if not execution or execution["executed_profile"]["reader"] not in PDF_READERS:
        return False
    found = _pdf_total_volume(_blocks(execution)[1])
    if found is None or found[0] != value:
        return False
    expected_blocks = {block["pointer"]: block for block in found[1]}
    if len(expected_blocks) != 4:
        return False
    for sid in ids:
        source, binding = sources[sid], bindings[sid]
        loc = source["locator"]
        block = expected_blocks.pop(loc["json_pointer"], None)
        if (
            block is None
            or source["kind"] != "attachment"
            or source["excerpt"] != block["text"]
            or loc["char_start"] != 0
            or loc["char_end"] != len(block["text"])
            or loc.get("page") != block["provenance"][0]["page_no"]
            or binding["docling_provenance"] != block["provenance"]
        ):
            return False
    return not expected_blocks and field["raw_values"] == [{"value": found[1][0]["text"]}]


def _pdf_table_field_supported(candidate, value, sources, bindings, executions, profile):
    field = candidate["field"]
    requirements = [
        item
        for item in profile["requirements"]
        if item["field_name"] == candidate["field_name"]
        and item["object_selector"] == candidate["object_id"]
        and item["source_kind"] == "attachment"
    ]
    if len(requirements) != 1:
        return False
    ids = field["source_ids"]
    artifact_ids = {bindings[sid]["artifact_id"] for sid in ids}
    if len(artifact_ids) != 1:
        return False
    execution = executions.get(next(iter(artifact_ids)))
    if not execution:
        return False
    actual = {sources[sid]["locator"]["json_pointer"] for sid in ids}
    for observed, unit, evidence in _attachment_document_totals(
        _blocks(execution)[1],
        requirements[0],
        execution["executed_profile"]["reader"],
    ):
        if (
            observed != value
            or unit != field.get("unit")
            or actual != {block["pointer"] for block in evidence}
        ):
            continue
        if field["raw_values"] != [{"value": evidence[0]["text"]}]:
            continue
        expected = {block["pointer"]: block for block in evidence}
        if all(
            sources[sid]["excerpt"] == expected[sources[sid]["locator"]["json_pointer"]]["text"]
            and bindings[sid]["docling_provenance"]
            == expected[sources[sid]["locator"]["json_pointer"]]["provenance"]
            for sid in ids
        ):
            return True
    return False


def _extracted_value_supported(
    candidate,
    sources,
    bindings,
    executions,
    profile,
    records,
    content_root,
    *,
    exact_evidence=False,
):
    field = candidate["field"]
    value = field["value"]
    if candidate["field_name"] in _BODY_ROLLUP_FIELDS:
        for sid in field["source_ids"]:
            source = sources[sid]
            if source["kind"] != "current_body":
                continue
            locator = source["locator"]
            execution = executions.get(bindings[sid]["artifact_id"])
            if execution is None:
                return False
            text = _pointer(_blocks(execution)[0], locator["json_pointer"])
            if _narrow_body_scope(text, locator["char_start"], locator["char_end"]):
                return False
    if profile["id"] == ATTACHMENT_WEIGHT_PROFILE:
        ids = field["source_ids"]
        refs = {bindings[sid]["artifact_id"] for sid in ids}
        requirements = [
            item
            for item in profile["requirements"]
            if item["field_name"] == candidate["field_name"]
            and item["object_selector"] == candidate["object_id"]
        ]
        execution = executions.get(next(iter(refs))) if len(refs) == 1 else None
        if execution is None or len(requirements) != 1:
            return False
        proof = table_weight(
            _blocks(execution)[1],
            execution,
            requirements[0]["weight_table"],
            candidate["field_name"],
        )
        if proof is None or len(ids) != len(proof[2]):
            return False
        number, unit, evidence = proof
        return (
            value == _decimal_value(_kg(number, unit))
            and field.get("unit") == "kg"
            and field["raw_values"] == [{"value": evidence[0]["text"]}]
            and all(sources[sid]["kind"] == "attachment" for sid in ids)
            and {(sources[sid]["locator"]["json_pointer"], sources[sid]["excerpt"]) for sid in ids}
            == {(block["pointer"], block["text"]) for block in evidence}
        )
    if profile["id"] == CARGO_ROW_PROFILE:
        ids = field["source_ids"]
        refs = {bindings[sid]["artifact_id"] for sid in ids}
        execution = executions.get(next(iter(refs))) if len(refs) == 1 else None
        requirements = [
            r
            for r in profile["requirements"]
            if r["field_name"] == candidate["field_name"]
            and r["object_selector"] == candidate["object_id"]
        ]
        if (
            not execution
            or len(requirements) != 1
            or any(sources[sid]["kind"] != "attachment" for sid in ids)
        ):
            return False
        proof = _packing_row_proof(packing_rows(_blocks(execution)[1], execution), requirements[0])
        if not proof or proof[1] != field.get("unit"):
            return False
        return _same_detail_proof(
            candidate, sources, (proof[0], [(b, 0, len(b["text"])) for b in proof[2]])
        )
    if profile["id"] == WEIGHT_INQUIRY_PROFILE and candidate["field_name"] in BODY_WEIGHT_FIELDS:
        ids = field["source_ids"]
        if candidate["field_name"] == "shipment_gross_weight" and _shipment_summary_supported(
            candidate,
            sources,
            bindings,
            executions,
            profile,
            records,
            content_root,
        ):
            return True
        if len(ids) != 1 or sources[ids[0]]["kind"] != "current_body" or field.get("unit") != "kg":
            return False
        locator = sources[ids[0]]["locator"]
        execution = executions.get(bindings[ids[0]]["artifact_id"])
        if execution is None or "/table_cells/" in locator["json_pointer"]:
            return False
        # Check the complete READ line so a fragment cannot omit a qualifier
        # such as "Line" or "per carton" to change the weight scope.
        source_text = _pointer(_blocks(execution)[0], locator["json_pointer"])
        accepted = _scoped_body_weight_facts(candidate["field_name"], source_text)
        if candidate["field_name"] == "shipment_chargeable_weight":
            accepted = (*accepted, *_agent_cargo_chargeable_facts(source_text))
        return any(
            value == observed
            and locator["char_start"] <= match.start() < match.end() <= locator["char_end"]
            for match, observed, _ in accepted
        )
    is_pdf_cargo = any(
        item.get("pdf_cargo")
        and item["field_name"] == candidate["field_name"]
        and item["object_selector"] == candidate["object_id"]
        for item in profile["requirements"]
    )
    is_pdf_row = any(
        item.get("pdf_row_anchor")
        and item["field_name"] == candidate["field_name"]
        and item["object_selector"] == candidate["object_id"]
        for item in profile["requirements"]
    )
    if is_pdf_cargo:
        supported = _pdf_cargo_field_supported(
            candidate, sources, bindings, executions, profile
        ) or _pdf_table_field_supported(candidate, value, sources, bindings, executions, profile)
    elif is_pdf_row:
        supported = _pdf_row_field_supported(
            candidate, value, sources, bindings, executions, profile
        )
    else:
        supported = (
            (
                (not exact_evidence or len(field["source_ids"]) == 1)
                and any(
                    observed == value and (unit is None or unit == field.get("unit"))
                    for sid in field["source_ids"]
                    for _, observed, unit in _facts(
                        candidate["field_name"],
                        sources[sid]["excerpt"],
                        source_kind=sources[sid]["kind"],
                    )
                )
            )
            or _body_agent_reply_supported(candidate, sources, bindings, executions)
            or _inquiry_detail_supported(
                candidate, sources, bindings, executions, records, content_root
            )
            or _shipment_summary_supported(
                candidate, sources, bindings, executions, profile, records, content_root
            )
            or _cargo_labelled_volume_supported(
                candidate, sources, bindings, executions, profile, records, content_root
            )
            or _body_agent_summary_supported(candidate, sources)
            or _compact_agent_gap_supported(
                candidate, sources, bindings, executions, profile, records, content_root
            )
            or _body_agent_field_supported(
                candidate, value, sources, bindings, executions, profile, records, content_root
            )
            or _body_total_field_supported(candidate, value, sources, bindings, executions, profile)
            or _pdf_total_volume_supported(candidate, value, sources, bindings, executions, profile)
            or _pdf_split_total_supported(candidate, value, sources, bindings, executions, profile)
            or _pdf_table_field_supported(candidate, value, sources, bindings, executions, profile)
        )
    return supported


def _inquiry_detail_supported(candidate, sources, bindings, executions, records, content_root):
    name = candidate["field_name"]
    if name not in AGENT_DETAIL_FIELDS:
        return False
    ids = candidate["field"]["source_ids"]
    refs = {bindings[sid]["artifact_id"] for sid in ids}
    mids = {sources[sid]["material_id"] for sid in ids}
    if (
        len(refs) != 1
        or len(mids) != 1
        or any(sources[sid]["kind"] != "current_body" for sid in ids)
    ):
        return False
    mid = next(iter(mids))
    if candidate["object_id"] != mid + "/document":
        return False
    execution = executions.get(next(iter(refs)))
    if execution is None or records[mid]["role"] != "current":
        return False
    raw = _read_content(Path(content_root), records[mid]["content"]).decode("utf-8-sig")
    scoped = list(_current_body_blocks(_blocks(execution)[1]))
    proof = inquiry_body_details(scoped, raw).get(name) or agent_body_detail_proofs(
        scoped, raw
    ).get(name)
    return proof is not None and _same_detail_proof(candidate, sources, proof)


def _same_detail_proof(candidate, sources, proof):
    value, evidence = proof
    field = candidate["field"]
    actual = [sources[sid]["locator"] for sid in field["source_ids"]]
    return (
        field["value"] == value
        and len(actual) == len(evidence)
        and {(x["json_pointer"], x["char_start"], x["char_end"]) for x in actual}
        == {(b["pointer"], start, end) for b, start, end in evidence}
    )


def _cargo_labelled_volume_supported(
    candidate, sources, bindings, executions, profile, records, content_root
):
    """Accept a complete current Cargo details heading and its adjacent CBM line."""
    field = candidate["field"]
    ids = field["source_ids"]
    if candidate["field_name"] != "line_volume" or len(ids) != 2 or field.get("unit") != "CBM":
        return False
    mid, artifact = sources[ids[0]]["material_id"], bindings[ids[0]]["artifact_id"]
    if (
        candidate["object_id"] != mid + "/document"
        or records[mid]["kind"] != "body"
        or records[mid]["role"] != "current"
        or any(
            sources[sid]["kind"] != "current_body"
            or sources[sid]["material_id"] != mid
            or bindings[sid]["artifact_id"] != artifact
            for sid in ids
        )
    ):
        return False
    requirements = [
        r
        for r in profile["requirements"]
        if r["field_name"] == "line_volume"
        and r["material_id"] == mid
        and r["object_selector"] == candidate["object_id"]
        and r["source_kind"] == "current_body"
    ]
    if len(requirements) != 1 or any(
        requirements[0].get(k) for k in ("object_anchor", "source_pattern")
    ):
        return False
    execution = executions.get(artifact)
    if execution is None:
        return False
    blocks = list(_current_body_blocks(_blocks(execution)[1]))
    headings = [
        i
        for i, (block, end) in enumerate(blocks)
        if end == len(block["text"])
        and not block.get("cell")
        and re.fullmatch(r"\s*cargo\s+details\s*[:：]?\s*", block["text"], re.I)
    ]
    if len(headings) != 1 or headings[0] + 1 >= len(blocks):
        return False
    heading, volume = blocks[headings[0]], blocks[headings[0] + 1]
    block, end = volume
    if (
        end != len(block["text"])
        or block.get("cell")
        or not _BODY_DECIMAL_VOLUME.fullmatch(block["text"])
    ):
        return False
    all_values = {
        value
        for item, limit in blocks
        for _, value, unit in _facts(
            "line_volume", item["text"][:limit], source_kind="current_body"
        )
        if unit == "CBM"
    }
    values = [
        (value, unit)
        for _, value, unit in _facts("line_volume", block["text"], source_kind="current_body")
    ]
    if all_values != {field["value"]} or values != [(field["value"], "CBM")]:
        return False
    raw = _read_content(Path(content_root), records[mid]["content"]).decode("utf-8-sig")
    current = current_body_text_check(raw, allow_quoted_duplicate=True)
    proof = [(heading[0], 0, len(heading[0]["text"])), (block, 0, len(block["text"]))]
    return current(heading[0]["text"] + " " + block["text"]) and _same_detail_proof(
        candidate, sources, (field["value"], proof)
    )


def _shipment_summary_supported(
    candidate, sources, bindings, executions, profile, records, content_root
):
    name = candidate["field_name"]
    if name not in SHIPMENT_SUMMARY_FIELDS:
        return False
    field = candidate["field"]
    ids = field["source_ids"]
    mid = sources[ids[0]]["material_id"]
    artifact = bindings[ids[0]]["artifact_id"]
    if (
        candidate["object_id"] != mid + "/document"
        or records[mid]["role"] != "current"
        or any(
            sources[s]["kind"] != "current_body"
            or sources[s]["material_id"] != mid
            or bindings[s]["artifact_id"] != artifact
            for s in ids
        )
        or not any(
            r["field_name"] == name
            and r["material_id"] == mid
            and r["object_selector"] == candidate["object_id"]
            and r["source_kind"] == "current_body"
            and not any(r.get(k) for k in ("object_anchor", "source_pattern"))
            for r in profile["requirements"]
        )
    ):
        return False
    execution = executions.get(artifact)
    if execution is None:
        return False
    raw = _read_content(Path(content_root), records[mid]["content"]).decode("utf-8-sig")
    current = current_body_text_check(raw, allow_quoted_duplicate=True)
    return any(
        n == name
        and unit == field.get("unit")
        and _same_detail_proof(candidate, sources, (value, proof))
        for n, value, unit, proof in shipment_summary_evidence(
            _current_body_blocks(_blocks(execution)[1]), current
        )
    )


def _compact_agent_gap_supported(
    candidate, sources, bindings, executions, profile, records, content_root
):
    name, field = candidate["field_name"], candidate["field"]
    ids = field["source_ids"]
    if name not in {
        "package_count",
        "package_type",
        "trade_terms",
        "line_volume",
        "destination",
        "cargo_type",
    } or len(ids) not in (1, 2, 3):
        return False
    sid = ids[0]
    source = sources[sid]
    mid = source["material_id"]
    if source["kind"] != "current_body" or candidate["object_id"] != mid + "/document":
        return False
    matching = [
        r
        for r in profile["requirements"]
        if r["field_name"] == name
        and r["material_id"] == mid
        and r["object_selector"] == candidate["object_id"]
        and r["source_kind"] == "current_body"
    ]
    if len(matching) != 1 or any(matching[0].get(k) for k in ("object_anchor", "source_pattern")):
        return False
    execution = executions.get(bindings[sid]["artifact_id"])
    if not execution or records[mid]["kind"] != "body" or records[mid]["role"] != "current":
        return False
    locator = source["locator"]
    raw = _read_content(Path(content_root), records[mid]["content"]).decode("utf-8-sig")
    current = current_body_text_check(raw, allow_quoted_duplicate=True)
    current_blocks = list(_current_body_blocks(_blocks(execution)[1]))
    if any(
        sources[x]["material_id"] != mid
        or sources[x]["kind"] != "current_body"
        or bindings[x]["artifact_id"] != bindings[sid]["artifact_id"]
        for x in ids
    ):
        return False
    blocks = [{**b, "text": b["text"][:end]} for b, end in current_blocks]
    if any(
        field_name == name
        and field.get("unit") == unit
        and _same_detail_proof(candidate, sources, (value, proof))
        for field_name, value, unit, proof in group_declaration_evidence(blocks, current)
    ):
        return True
    if len(ids) in (2, 3):
        blocks = [b for b, end in current_blocks if end == len(b["text"])]
        structured_proofs = quote_table_gap_evidence if len(ids) == 2 else split_cargo_gap_evidence
        if any(
            name == field_name
            and field.get("unit") == unit
            and _same_detail_proof(candidate, sources, (value, proof))
            for field_name, value, unit, proof in structured_proofs(blocks, current)
        ):
            return True
    citations = [sources[x]["locator"] for x in ids]
    if any(c["json_pointer"] != locator["json_pointer"] for c in citations):
        return False
    spans = {(c["char_start"], c["char_end"]) for c in citations}
    for block, end in current_blocks:
        if block["pointer"] != locator["json_pointer"] or block.get("cell"):
            continue
        text = block["text"][:end]
        for field_name, value, unit, start, stop in compact_gap_evidence(text, current):
            if (field_name, value, unit) != (name, field["value"], field.get("unit")):
                continue
            supported = {(start, stop)}
            if name in {"package_type", "line_volume"} and (
                window := compact_cargo_context(text, start, stop, current)
            ):
                supported.add(window)
            if len(spans) == len(ids) and spans <= supported:
                return True
    return False


def _packing_row_proof(rows, requirement):
    return next(
        (
            r["facts"].get(requirement["field_name"])
            for r in rows
            if r["selector"] == requirement["cargo_table_row"]
        ),
        None,
    )


def _body_agent_reply_supported(candidate, sources, bindings, executions):
    """Prove the destination or outer package count of one complete compact reply."""
    name, field = candidate["field_name"], candidate["field"]
    if (
        name not in {"destination", "package_count"}
        or len(field["source_ids"]) != 1
        or field.get("unit") is not None
    ):
        return False
    sid = field["source_ids"][0]
    source = sources[sid]
    if (
        source["kind"] != "current_body"
        or candidate["object_id"] != source["material_id"] + "/document"
        or not re.fullmatch(r"/(?:text|texts/\d+/text)", source["locator"]["json_pointer"])
    ):
        return False
    execution = executions.get(bindings[sid]["artifact_id"])
    if execution is None:
        return False
    locator = source["locator"]
    raw = _pointer(_blocks(execution)[0], locator["json_pointer"])
    # Docling may combine the summary and dimensions in one block. Allow only
    # dimension lines after the summary, so a citation cannot hide a qualifier.
    lines = raw.splitlines(keepends=True)
    if not lines or any(
        line.strip() and not _REPLY_DIMENSIONS.fullmatch(line.strip()) for line in lines[1:]
    ):
        return False
    first = lines[0]
    if not (
        locator["char_start"] <= len(first) - len(first.lstrip())
        and len(first.rstrip()) <= locator["char_end"]
    ):
        return False
    match = _REPLY_CARGO_SUMMARY.fullmatch(first.strip())
    if not match:
        return False
    observed = match["destination"] if name == "destination" else int(match["count"])
    return observed == field["value"]


def _body_agent_summary_supported(candidate, sources):
    """A complete carton/weight/volume reply proves only its package type and volume."""
    name, field = candidate["field_name"], candidate["field"]
    if name not in {"package_type", "line_volume"} or len(field["source_ids"]) != 1:
        return False
    source = sources[field["source_ids"][0]]
    if (
        source["kind"] != "current_body"
        or candidate["object_id"] != source["material_id"] + "/document"
        or not re.fullmatch(r"/(?:text|texts/\d+/text)", source["locator"]["json_pointer"])
    ):
        return False
    match = _COMPACT_CARGO_SUMMARY.fullmatch(source["excerpt"].strip())
    if not match:
        return False
    value, unit = (
        ("carton", None) if name == "package_type" else (_decimal_value(match["volume"]), "CBM")
    )
    return value == field["value"] and unit == field.get("unit")


def _body_agent_field_supported(
    candidate, value, sources, bindings, executions, profile, records, content_root
):
    field = candidate["field"]
    name = candidate["field_name"]
    paired = name in {"line_gross_weight", "line_volume"}
    if name not in {
        "package_count",
        "destination",
        "trade_terms",
        "line_gross_weight",
        "line_volume",
    } or (
        len(field["source_ids"]) != (2 if paired else 1)
        or (not paired and field.get("unit") is not None)
    ):
        return False
    sid = field["source_ids"][0]
    source, binding = sources[sid], bindings[sid]
    mid = source["material_id"]
    matching = [
        item
        for item in profile["requirements"]
        if item["field_name"] == name
        and item["source_kind"] == "current_body"
        and item["material_id"] == mid
        and item["object_selector"] == candidate["object_id"] == mid + "/document"
    ]
    execution = executions.get(binding["artifact_id"])
    record = records[mid]
    if (
        len(matching) != 1
        or execution is None
        or record["kind"] != "body"
        or record["role"] != "current"
        or record.get("format") not in {"html", "text"}
        or execution["executed_profile"]["reader"] not in {"DoclingHtml", "DoclingText"}
    ):
        return False
    raw = _read_content(Path(content_root).resolve(strict=True), record["content"])
    blocks = _blocks(execution)[1]
    if any(_scope_analysis(matching[0], block["text"])[1] for block in blocks):
        return False
    locator = source["locator"]
    if not any(
        block["pointer"] == locator["json_pointer"]
        and offset <= locator["char_start"] < locator["char_end"] <= offset + len(text)
        for block, offset, text in _scoped_blocks(matching[0], blocks)
    ):
        return False
    original = raw.decode("utf-8-sig")
    if paired:
        ids = field["source_ids"]
        if any(
            sources[x]["kind"] != "current_body"
            or bindings[x]["artifact_id"] != binding["artifact_id"]
            for x in ids
        ):
            return False
        selected = {
            (
                sources[x]["locator"]["json_pointer"],
                sources[x]["locator"]["char_start"],
                sources[x]["locator"]["char_end"],
            )
            for x in ids
        }
        current = current_body_text_check(original)
        return any(
            value == expected
            and field.get("unit") == unit
            and selected == {(b["pointer"], 0, len(b["text"])) for b in pair}
            and current(pair[1]["text"] + " " + pair[0]["text"])
            for expected, unit, pair in _body_total_fields(blocks, matching[0], agent_labels=True)
        )
    if name in {"destination", "trade_terms"}:
        if name == "trade_terms" and source["kind"] == "current_body":
            current = current_body_text_check(original, allow_quoted_duplicate=True)
            if any(
                block["pointer"] == locator["json_pointer"]
                and term == value
                and (start, end) == (locator["char_start"], locator["char_end"])
                for block, boundary in _current_body_blocks(blocks)
                if not block.get("cell")
                for start, end, term in declared_shipment_terms(block["text"][:boundary], current)
            ):
                return True
        return source["kind"] == "current_body" and any(
            value == expected
            and block["pointer"] == locator["json_pointer"]
            and source["excerpt"] == block["text"]
            and locator["char_start"] == 0
            and locator["char_end"] == len(block["text"])
            for block, expected in current_route_destinations(blocks, original, field=name)
        )
    return revised_count_supported(value, source, blocks, original)


def _body_total_field_supported(candidate, value, sources, bindings, executions, profile):
    requirements = [
        item
        for item in profile["requirements"]
        if item.get("source_kind") == "current_body"
        and item["field_name"] == candidate["field_name"]
        and item["object_selector"] == candidate["object_id"]
    ]
    ids = candidate["field"]["source_ids"]
    if len(requirements) != 1 or not ids:
        return False
    artifact_ids = {bindings[sid]["artifact_id"] for sid in ids}
    if len(artifact_ids) != 1:
        return False
    execution = executions.get(next(iter(artifact_ids)))
    if not execution or execution["executed_profile"]["reader"] not in {
        "DoclingHtml",
        "DoclingMarkdown",
    }:
        return False
    actual = {(sources[sid]["locator"]["json_pointer"], sources[sid]["excerpt"]) for sid in ids}
    for observed, unit, evidence in _body_total_fields(_blocks(execution)[1], requirements[0]):
        if (
            observed == value
            and unit == candidate["field"].get("unit")
            and actual == {(block["pointer"], block["text"]) for block in evidence}
            and candidate["field"]["raw_values"] == [{"value": evidence[0]["text"]}]
        ):
            return all(sources[sid]["kind"] == "current_body" for sid in ids)
    return False


def _pdf_split_total_supported(candidate, value, sources, bindings, executions, profile):
    field = candidate["field"]
    if len(field["source_ids"]) != 2 or not any(
        item.get("pdf_split_total") is True
        and item["field_name"] == candidate["field_name"]
        and item["object_selector"] == candidate["object_id"]
        for item in profile["requirements"]
    ):
        return False
    ids = field["source_ids"]
    artifact_ids = {bindings[sid]["artifact_id"] for sid in ids}
    if len(artifact_ids) != 1:
        return False
    execution = executions.get(next(iter(artifact_ids)))
    if not execution or execution["executed_profile"]["reader"] not in PDF_READERS:
        return False
    found = _pdf_split_total(_blocks(execution)[1], candidate["field_name"], evidence_only=True)
    if found is None or found[0] != value or found[1] != field.get("unit"):
        return False
    expected_blocks = {block["pointer"]: block for block in found[2]}
    if len(expected_blocks) != 2:
        return False
    for sid in ids:
        source, binding = sources[sid], bindings[sid]
        loc = source["locator"]
        block = expected_blocks.pop(loc["json_pointer"], None)
        if (
            block is None
            or source["kind"] != "attachment"
            or source["excerpt"] != block["text"]
            or loc["char_start"] != 0
            or loc["char_end"] != len(block["text"])
            or loc.get("page") != block["provenance"][0]["page_no"]
            or binding["docling_provenance"] != block["provenance"]
        ):
            return False
    return not expected_blocks and field["raw_values"] == [{"value": found[2][0]["text"]}]


def _pointer(document, pointer):
    value = document
    for part in pointer.lstrip("/").split("/"):
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def _decimal_value(number):
    number = Decimal(number)
    return int(number) if number == number.to_integral_value() else float(number)


def _kg(number, unit):
    factor = {
        "kg": "1",
        "kgs": "1",
        "kilogram": "1",
        "kilograms": "1",
        "千克": "1",
        "公斤": "1",
        "g": "0.001",
        "gram": "0.001",
        "grams": "0.001",
        "克": "0.001",
        "lb": "0.45359237",
        "lbs": "0.45359237",
        "pound": "0.45359237",
        "pounds": "0.45359237",
        "磅": "0.45359237",
    }[unit.lower()]
    return Decimal(number) * Decimal(factor)


def _inquiry_values(field_name, text):
    value = text.strip()
    if not value or value.lower() in {"n/a", "unknown", "tbc", "待定", "未知"}:
        return []
    if field_name == "trade_terms":
        terms = [match.group().upper() for match in _INCOTERM.finditer(value)]
        if re.search(r"\bex\s*-?\s*works?\b", value, re.I):
            terms.append("EXW")
        return list(dict.fromkeys(terms))
    return [value]


_ROUTE_PLACE = r"(?:[A-Z]{3,}|[A-Z][a-z]+(?:[ -][A-Z][a-z]+){0,2})"
_BODY_DIRECTION = re.compile(
    rf"(?i:\bfrom\s+)(?P<origin>{_ROUTE_PLACE})(?i:\s+to\s+)(?P<destination>{_ROUTE_PLACE})"
    r"(?i:(?:\s+(?:airport|port))?)(?=\s*(?:[.!?;]|$))"
)
_DESTINATION_FROM_AIRPORT_OPTIONS = re.compile(
    r"(?i:\bfrom[ \t]+)(?:[A-Z]{3}[ \t]*/[ \t]*){1,3}[A-Z]{3}(?i:[ \t]+to[ \t]+)"
    r"(?P<destination>[A-Z]{3})(?i:[ \t]+airport)?[ \t]*[.!?]?[ \t]*$",
)
_DESTINATION_FROM_CONTAINER_QUOTE = re.compile(
    rf"(?i:\bfrom[ \t]+){_ROUTE_PLACE}(?i:[ \t]+to[ \t]+)(?P<destination>{_ROUTE_PLACE})"
    r"(?i:[ \t]+details?[ \t]+(?:is[ \t]+|are[ \t]+)?as[ \t]+below)[ \t]*[:：]?[ \t]*$",
)
_NEW_CARGO_AIRPORT = re.compile(
    rf"(?i:\b(?:new|current)[ \t]+(?:cargo|shipment)[ \t]+to[ \t]+){_ROUTE_PLACE}"
    rf"[ \t]+\((?P<destination>[A-Z]{{3}})\)(?:[ \t]+(?P<term>(?i:{_INCOTERM.pattern}))(?i:[ \t]+terms?))?"
    r"(?i:[ \t]+cargo[ \t]+ready[ \t]+\d{1,2}/\d{1,2}(?:/\d{2,4})?)?[ \t]*[.!?]?[ \t]*$",
)
_ES_PLACE = r"[A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ]+(?:[ \t]+[A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ]+){0,2}"
_SPANISH_AIR_DESTINATION = re.compile(
    rf"(?i:^[ \t]*(?:por[ \t]+)?favor[ \t]+cotizar[ \t]+[^\r\n.!?]{{0,80}}?"
    rf"por[ \t]+(?:v[ií]a[ \t]+)?a[eé]re[oa][ \t]+a[ \t]+)"
    rf"(?P<destination>{_ES_PLACE}(?:[ \t]+[-–][ \t]+{_ES_PLACE})?)[ \t]*[.!?]?[ \t]*$",
)
_TO_NAMED_AIRPORT = re.compile(
    rf"[ \t]*(?i:to)[ \t]+(?P<place>{_ROUTE_PLACE})[ \t]+(?i:airport)[ \t]*[.!]?[ \t]*",
)
_CARGO_TO_CODE = re.compile(
    r"(?i:\b(?:cargo|shipment|freight)[ \t]+to[ \t]+)(?P<code>[A-Z]{3})"
    r"(?:[ \t]*,[ \t]*(?P<qualifier>[A-Za-z]+(?:[ \t]+[A-Za-z]+){0,2}))?[ \t]*[.:：]?[ \t]*$",
)
_ROUTE_QUALIFIER = re.compile(
    r"\b(?:not|never|without|instead|rather than|previous|old|no)\b", re.I
)
_REQUEST_TERM = re.compile(
    rf"\b(?:quote|(?:give|send|provide|share|offer)\b[^\r\n.!?]{{0,60}}?\b(?:rates?|quote|quotation))"
    rf"[ \t]+(?P<term>{_INCOTERM.pattern})[ \t]+terms?[ \t]*[.!?]?[ \t]*$",
    re.I,
)
_TERM_OPTIONS = re.compile(r"\b(?:or|either|option[s]?|alternativ\w*|whether|if)\b|备选|或者", re.I)
_REPLY_CARGO_SUMMARY = re.compile(
    r"(?:(?:到|(?i:to)[ \t]+)(?P<destination>[A-Z]{3})[ \t]+|"
    r"(?:此票|本票)(?:货物)?数据(?:有)?更新[ \t]*[:：][ \t]*)"
    r"(?P<count>[1-9]\d*)[ \t]*(?i:plts?|pallets?|pkgs?|packages?|ctns?|cartons?)[ \t]*/[ \t]*"
    r"\d+(?:\.\d+)?[ \t]*(?i:kgs?|lbs?)[ \t]*/[ \t]*\d+(?:\.\d+)?[ \t]*(?i:cbm|m[³3])[ \t]*[.。]?",
)
_REPLY_DIMENSIONS = re.compile(
    r"\d+(?:\.\d+)?(?:[ \t]*[*×xX][ \t]*\d+(?:\.\d+)?){2}(?:[ \t]*(?:mm|cm|m))?", re.I
)
_AGENT_CONDITION = re.compile(
    r"\b(?:if|unless|whether|subject[ \t]+to|provided[ \t]+that|or|either|alternativ\w*|options?)\b"
    r"|如果|若(?:为|按|走|是)|备选|或者|或[ \t]*到",
    re.I,
)
_AGENT_TERM_ALTERNATIVES = re.compile(rf"{_INCOTERM.pattern}[ \t]*/[ \t]*{_INCOTERM.pattern}", re.I)
_AGENT_ROUTE_ALTERNATIVES = re.compile(r"(?:到|\bto[ \t]+)[ \t]*[A-Z]{3}[ \t]*/[ \t]*[A-Z]{3}\b")
_AGENT_COUNT_UNIT = re.compile(
    r"(?<!\d)\d+[ \t]*(?P<kind>plts?|pallets?|pkgs?|packages?|pcs|pieces?)\b", re.I
)
_QUOTE_REQUEST = re.compile(
    r"^\s*(?:please|pls\.?|(?:can|could|would)\s+you)\b[^\r\n]{0,90}?"
    r"\b(?:give|send|provide|share|offer|quote|help)\b",
    re.I,
)
_TERM_FREIGHT_QUOTE = re.compile(
    rf"{_INCOTERM.pattern}[ \t]+(?:air[ \t]+)?(?:charges|rates?|quotation|quote)\b", re.I
)
_TERM_CODE_ROUTE = re.compile(rf"{_INCOTERM.pattern}[ \t]+[A-Z]{{3}}[ \t]*[-–][ \t]*[A-Z]{{3}}\.?")
_BODY_PACKING_HEAD = re.compile(
    r"(?:[ \t]*(?:data|cargo[ \t]+details)[ \t]*[:：])?[ \t]*"
    r"(?P<count>[1-9]\d*[ \t]*(?:cartons?|ctns?|pallets?))\b(?P<tail>[^\r\n]*)",
    re.I,
)
_PACKING_MASS_TAIL = re.compile(
    r"(?:[ \t]*[/,，;–-][ \t]*|[ \t]+)\d+(?:[.,]\d+)?[ \t]*(?:kgs?|lbs?|g|k)\b",
    re.I,
)
_SLASH_CARGO_SUMMARY = re.compile(
    r"[ \t]*(?P<count>[1-9]\d*)[ \t]*(?:pkgs?|packages?|ctns?|cartons?|pallets?)[ \t]*/[ \t]*"
    r"(?:(?P<gross>gross[ \t]+weight)[ \t]*[:：]?[ \t]*)?"
    r"(?P<mass>\d+(?:\.\d+)?)[ \t]*(?P<mass_unit>kgs?|lbs?|g|k)[ \t]*/[ \t]*"
    r"(?P<volume>\d+(?:\.\d+|,\d{1,4})?)[ \t]*(?:cbm|m³|m3)[ \t]*\.?[ \t]*",
    re.I,
)
_BODY_DECIMAL_VOLUME = re.compile(
    r"^[ \t]*(?:cbm[ \t]*[:：][ \t]*(?P<label>\d+,\d{1,4})(?:[ \t]*cbm)?|"
    r"(?:(?:total[ \t]+)?volume|measures?)[ \t]*[:：][ \t]*(?P<value>\d+,\d{1,4})[ \t]*(?:cbm|m³|m3))[ \t]*$",
    re.I | re.M,
)
_COMMA_FORM_FIELDS = {
    "origin": r"origin airport",
    "destination": r"destination airport",
    "trade_terms": r"incoterms?",
    "line_gross_weight": r"total gross weight",
    "line_volume": r"total volume",
}


def _comma_body_form(field_name, text):
    """Read explicitly labelled single-line form fields flattened by Docling."""
    if field_name not in _COMMA_FORM_FIELDS:
        return
    label = _COMMA_FORM_FIELDS[field_name]
    for match in re.finditer(
        rf"^[ \t]*{label}[ \t]*[,，][ \t]*(?P<value>[^\r\n]+?)[ \t]*(?=\r?$)",
        text,
        re.I | re.M,
    ):
        value = match["value"].strip()
        if field_name in {"origin", "destination"}:
            place = re.fullmatch(
                r"([A-Z]{3})(?:[ \t]+[A-Z][a-z]+(?:[ \t-]+[A-Z][a-z]+){0,2})?", value
            )
            if place:
                yield match, place[1], None
        elif field_name == "trade_terms":
            if _INCOTERM.fullmatch(value):
                yield match, value.upper(), None
        elif field_name == "line_gross_weight":
            mass = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(kgs?|g|lbs?)", value, re.I)
            if mass:
                yield match, _decimal_value(_kg(mass[1], mass[2])), "kg"
        else:
            volume = re.fullmatch(r"(\d+(?:\.\d+|,\d{1,4})?)\s*(?:cbm|m³|m3)", value, re.I)
            if volume:
                yield match, _decimal_value(volume[1].replace(",", ".")), "CBM"


def _body_direction(field_name, text):
    label = "AOL" if field_name == "origin" else "AOD"
    for line in re.finditer(r"^[^\r\n]+$", text, re.M):
        phrase = line.group()
        if match := re.fullmatch(
            rf"\s*(?i:{label})\s*[:：]\s*(?P<place>{_ROUTE_PLACE})\s*", phrase
        ):
            yield line, match["place"], None
        elif re.search(r"\b(?:freight|shipment|shipping|rates?|quot(?:e|ation))\b", phrase, re.I):
            routes = list(_BODY_DIRECTION.finditer(phrase))
            if len(routes) == 1 and not re.search(
                r"\b(?:not|never|without)\b", phrase[: routes[0].start()], re.I
            ):
                yield line, routes[0][field_name], None


def _explicit_body_destination(text):
    """Extract one stated endpoint without selecting between origin airports."""
    for line in re.finditer(r"^[^\r\n]+$", text, re.M):
        phrase = line.group()
        if _ROUTE_QUALIFIER.search(phrase) or _TERM_OPTIONS.search(phrase):
            continue
        if match := _NEW_CARGO_AIRPORT.search(phrase) or _SPANISH_AIR_DESTINATION.fullmatch(phrase):
            yield line, match["destination"], None
        elif match := _TO_NAMED_AIRPORT.fullmatch(phrase):
            place = match["place"]
            yield line, place if re.fullmatch(r"[A-Z]{3}", place) else f"{place} airport", None
        elif not re.search(r"\bfrom\b", phrase, re.I) and (match := _CARGO_TO_CODE.search(phrase)):
            if not re.search(r"\band\b", match["qualifier"] or "", re.I):
                yield line, match["code"], None
        elif re.search(r"\b(?:freight|shipment|shipping|air rates)\b", phrase, re.I):
            if match := _DESTINATION_FROM_AIRPORT_OPTIONS.search(phrase):
                yield line, match["destination"], None
        elif re.search(
            r"\b(?:price|quote|rates?)\b.*\b\d+[ \t]*[x×][ \t]*(?:20|40|45)\b", phrase, re.I
        ):
            if match := _DESTINATION_FROM_CONTAINER_QUOTE.search(phrase):
                yield line, match["destination"], None


_ADDRESS_CITY = re.compile(
    r"(?:^|[,，])[ \t]*(?P<city>[A-Za-z][A-Za-z'-]*(?:[ \t]+[A-Za-z][A-Za-z'-]*){0,2})[ \t]+City\b"
    r"(?=[ \t]*(?:[,，]|$|[A-Za-z]+(?:[ \t]+[A-Za-z]+)?[ \t]+Province\b))",
    re.I | re.M,
)
_LOCATION_NONVALUE = re.compile(
    r"\b(?:unknown|tbc|tbd|pending|same|above|or|and|instead|not|via|address|road|street|"
    r"company|warehouse|loading|city of origin|country of origin|incoterms?|destination)\b"
    r"|待定|未知|公司|地址|或者|备选",
    re.I,
)


def _typed_location_value(field_name, label, raw_value):
    """Keep the address city only when the enclosing label establishes its role."""
    value = raw_value.strip()
    pickup_address = re.fullmatch(_PICKUP_ADDRESS_LABEL, label, re.I)
    if pickup_address and re.match(
        r"(?:company|customer|billing|contact|sender)(?:[ \t]+address)?[ \t]*[:：]|签名[ \t]*[:：]",
        value,
        re.I,
    ):
        return None
    if pickup_address or (
        field_name == "cargo_origin" and re.fullmatch(r"ship[ \t]+from", label, re.I)
    ):
        cities = {
            match["city"].casefold(): match["city"] for match in _ADDRESS_CITY.finditer(value)
        }
        if cities:
            return next(iter(cities.values())) if len(cities) == 1 else None
        if pickup_address:
            return None
    if not re.fullmatch(r"[^\W\d_]+(?:[ \t'-]+[^\W\d_]+){0,5}", value) or _LOCATION_NONVALUE.search(
        value
    ):
        return None
    if field_name == "departure_port":
        if match := re.fullmatch(r"([A-Z]{3})(?:[ \t]+(?i:airport|port))?", value):
            return match[1]
    else:
        if re.search(r"\b(?:airport|port)\b|机场|港口", value, re.I):
            return None
        if field_name == "pickup_city":
            return re.sub(r"[ \t]+City$", "", value, flags=re.I)
    return value


def _typed_location_facts(field_name, text):
    label = _LOCATION_LABELS[field_name]
    for match in re.finditer(
        rf"^[ \t]*(?P<label>{label})[ \t]*[:：,，][ \t]*(?:\r?\n[ \t]*)?"
        r"(?P<value>[^\r\n]+?)[ \t]*(?=\r?$)",
        text,
        re.I | re.M,
    ):
        if value := _typed_location_value(field_name, match["label"], match["value"]):
            yield match, value, None


_WEIGHT_UNIT_TOKEN = r"kg|kgs|kilograms?|g|grams?|lb|lbs|pounds?|千克|公斤|克|磅"
_WEIGHT_KINDS = {
    "gross_weight": r"gross[ \t]+(?:weight|wt)|g\.[ \t]*w\.?|毛重",
    "net_weight": r"net[ \t]+(?:weight|wt)|n\.[ \t]*w\.?|净重",
    "chargeable_weight": r"chargeable[ \t]+(?:weight|wt)|c\.?[ \t]*w\.?|计费重(?:量)?",
}
_ITEM_WEIGHT_SCOPE = (
    r"per[ \t]+(?:unit|item|carton|box)|each[ \t]+(?:unit|item|carton|box)|每(?:箱|件)|单(?:箱|件)"
)
_SCOPED_WEIGHT_VALUE = rf"(?P<number>\d+(?:\.\d+|,\d{{1,2}})?)[ \t]*(?P<unit>{_WEIGHT_UNIT_TOKEN})"
_CARGO_DETAILS_CW = re.compile(
    rf"cargo[ \t]+details[ \t]*[:：][ \t]*[1-9]\d*[ \t]*(?:ctns?|cartons?|pallets?|plts?|pkgs?|packages?)"
    rf"[ \t]+\d+(?:\.\d+)?[ \t]*kgs?[ \t]+\d+(?:\.\d+)?[ \t]*(?:cbm|m[³3])"
    rf"[ \t]+c\.?[ \t]*w\.?[ \t]*[:：][ \t]*{_SCOPED_WEIGHT_VALUE}[ \t]*\.?",
    re.I,
)
_AGENT_CARGO_DETAILS_CW = re.compile(
    rf"cargo[ \t]+details[ \t]*[:：][ \t]*[1-9]\d*[ \t]*(?:ctns?|cartons?|pallets?|plts?|pkgs?|packages?)"
    rf"[ \t]*[/,，][ \t]*\d+(?:\.\d+)?[ \t]*kgs?[ \t]*[/,，][ \t]*"
    rf"\d+(?:\.\d+)?[ \t]*(?:cbm|m[³3])[ \t]+(?:chargeable[ \t]+weight|c\.?w\.?)"
    rf"[ \t]*[:：][ \t]*{_SCOPED_WEIGHT_VALUE}[ \t]*\.?",
    re.I,
)


def _agent_cargo_chargeable_facts(text):
    """Explicit chargeable weight at the end of a same-shipment cargo summary."""
    for line in re.finditer(r"^[^\r\n]+$", text, re.M):
        if match := _AGENT_CARGO_DETAILS_CW.fullmatch(line.group().strip()):
            yield line, _decimal_value(_kg(match["number"].replace(",", "."), match["unit"])), "kg"


def _scoped_body_weight_facts(name, text):
    scope, kind = name.split("_", 1)
    label = _WEIGHT_KINDS[kind]
    if scope == "unit":
        label = (
            rf"(?:{_ITEM_WEIGHT_SCOPE})[ \t]*(?:{label})|(?:{label})[ \t]+(?:{_ITEM_WEIGHT_SCOPE})"
        )
    else:
        label = rf"(?:(?:shipment|total)[ \t]+|整票|总)?(?:{label})"
    pattern = re.compile(rf"(?:{label})[ \t]*[:：,，][ \t]*{_SCOPED_WEIGHT_VALUE}[ \t]*\.?", re.I)
    for line in re.finditer(r"^[^\r\n]+$", text, re.M):
        text_line = line.group().strip()
        match = pattern.fullmatch(text_line)
        if not match and name == "shipment_chargeable_weight":
            match = _CARGO_DETAILS_CW.fullmatch(text_line)
        if match:
            yield line, _decimal_value(_kg(match["number"].replace(",", "."), match["unit"])), "kg"
        elif (
            name == "shipment_gross_weight"
            and (summary := _SLASH_CARGO_SUMMARY.fullmatch(text_line))
            and summary["gross"]
            and summary["mass_unit"].lower() != "k"
        ):
            yield line, _decimal_value(_kg(summary["mass"], summary["mass_unit"])), "kg"


_BODY_ROLLUP_FIELDS = {
    "ctns",
    "package_count",
    "package_type",
    "line_volume",
    "line_gross_weight",
    "line_net_weight",
    "chargeable_weight",
}
_PACKAGE_SCOPE_EN = r"(?:cartons?|ctns?|boxes?|pallets?|packages?|pieces?|units?|items?)\b"
_PACKAGE_SCOPE_ZH = r"(?:外箱|纸箱|箱|托盘|托|包装|件|个)"
_NARROW_BODY_PREFIX = re.compile(
    rf"(?:\b(?:per|each)[ \t]+{_PACKAGE_SCOPE_EN}(?:[ \t]+(?:contains?|has))?"
    rf"|每[ \t]*{_PACKAGE_SCOPE_ZH}(?:共|含|装)?)[ \t]*[:：]?[ \t]*$",
    re.I,
)
_NARROW_BODY_SUFFIX = re.compile(
    rf"[ \t]*\(?[ \t]*(?:per[ \t]+{_PACKAGE_SCOPE_EN}"
    rf"|each(?=[ \t]*(?:[.,，;；)]|$))"
    rf"|[/／][ \t]*(?:{_PACKAGE_SCOPE_EN}|{_PACKAGE_SCOPE_ZH})|每[ \t]*{_PACKAGE_SCOPE_ZH})",
    re.I,
)


def _narrow_body_scope(text, start, end):
    """A quantity qualified by its parent package is not a rollup."""
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    tail = text[end : line_end if line_end >= 0 else len(text)].rstrip("\r")
    return bool(
        _NARROW_BODY_PREFIX.search(text[line_start:start]) or _NARROW_BODY_SUFFIX.match(tail)
    )


def _body_packing_kind(line):
    match = _BODY_PACKING_HEAD.fullmatch(line)
    if not match or _ROUTE_QUALIFIER.search(line) or _TERM_OPTIONS.search(line):
        return None
    tail = match["tail"]
    if re.fullmatch(r"[ \t]*[.:：]?[ \t]*", tail):
        return _package_expression(match["count"])[1]
    if not _PACKING_MASS_TAIL.match(tail) or re.search(
        rf"\b(?:per|each)\b|每|\d+[ \t]*{_PACKAGE_SCOPE_EN}",
        tail,
        re.I,
    ):
        return None
    return _package_expression(match["count"])[1]


def _quoted_request_term(line):
    terms = {item.group().upper() for item in _INCOTERM.finditer(line)}
    if len(terms) != 1 or _ROUTE_QUALIFIER.search(line) or _TERM_OPTIONS.search(line):
        return None
    request, quote = _QUOTE_REQUEST.match(line), _TERM_FREIGHT_QUOTE.search(line)
    if _TERM_CODE_ROUTE.fullmatch(line.strip()) or (
        request and quote and request.end() <= quote.start()
    ):
        return next(iter(terms))
    return None


def _adjacent_inquiry_value(field_name, label, value):
    """Only accept neighboring text with an explicit role for the standalone value."""
    label, value = label.strip(), value.strip()
    if (
        field_name == "destination"
        and re.fullmatch(rf"(?:{_INQUIRY_LABELS['destination']})[ \t]*[:：]", label, re.I)
        and re.fullmatch(r"[A-Z]{3}", value)
    ):
        return value
    if (
        field_name == "trade_terms"
        and _INCOTERM.fullmatch(value)
        and _QUOTE_REQUEST.match(label)
        and re.search(r"\b(?:air|freight|shipment|rates?)\b", label, re.I)
        and not (
            _INCOTERM.search(label) or _ROUTE_QUALIFIER.search(label) or _TERM_OPTIONS.search(label)
        )
    ):
        return value
    return None


def _facts(field_name, text, *, source_kind=None):
    for match, value, unit in _field_facts(field_name, text, source_kind=source_kind):
        if (
            source_kind == "current_body"
            and field_name in _BODY_ROLLUP_FIELDS
            and _narrow_body_scope(text, match.start(), match.end())
        ):
            continue
        yield match, value, unit


def _field_facts(field_name, text, *, source_kind=None):
    if field_name in _LOCATION_LABELS:
        if source_kind == "current_body":
            yield from _typed_location_facts(field_name, text)
        return
    if source_kind == "current_body":
        yield from _comma_body_form(field_name, text)
        if field_name in {"package_count", "line_gross_weight", "line_volume"}:
            for line in re.finditer(r"^[^\r\n]+$", text, re.M):
                if summary := _SLASH_CARGO_SUMMARY.fullmatch(line.group()):
                    if field_name == "package_count":
                        yield line, int(summary["count"]), None
                    elif field_name == "line_volume":
                        yield line, _decimal_value(summary["volume"].replace(",", ".")), "CBM"
                    elif summary["gross"] and summary["mass_unit"].lower() != "k":
                        yield line, _decimal_value(_kg(summary["mass"], summary["mass_unit"])), "kg"
    if source_kind == "current_body" and field_name in {"package_count", "package_type"}:
        token = re.compile(
            r"\b\d+\s*(?:wooden\s+(?:box(?:es)?|cases?|crates?)|cartons?|ctns?|pallets?|packages?)\b",
            re.I,
        )
        for line in re.finditer(r"^[^\r\n]+$", text, re.M):
            if field_name == "package_type" and (kind := _body_packing_kind(line.group())):
                yield line, kind, None
                continue
            matches = list(token.finditer(line.group()))
            if len(matches) != 1 or not re.match(r"\s*total\s*[:：]?\s*", line.group(), re.I):
                continue
            if _narrow_body_scope(line.group(), *matches[0].span()):
                continue
            count, kind = _package_expression(matches[0].group())
            value = count if field_name == "package_count" else kind
            if value is not None:
                yield line, value, None
        if field_name == "package_type":
            return
    if field_name in _INQUIRY_LABELS:
        if source_kind == "current_body" and field_name in {"destination", "trade_terms"}:
            for line in re.finditer(r"^[^\r\n]+$", text, re.M):
                pair = re.compile(r"(?P<label>[^\r\n]+)\r?\n(?P<value>[^\r\n]+)").match(
                    text, line.start()
                )
                if pair and (
                    observed := _adjacent_inquiry_value(field_name, pair["label"], pair["value"])
                ):
                    yield pair, observed, None
        if source_kind == "current_body" and field_name == "cargo_type":
            for line in re.finditer(
                r"^[ \t]*general[ \t]+cargo[.!。]?[ \t]*(?=\r?$)", text, re.I | re.M
            ):
                yield line, "general cargo", None
        pattern = (
            rf"^[ \t]*(?:{_INQUIRY_LABELS[field_name]})[ \t]*[:：][ \t]*(?P<value>[^\r\n]+?)[ \t]*$"
        )
        for match in re.finditer(pattern, text, re.I | re.M):
            for value in _inquiry_values(field_name, match.group("value")):
                yield match, value, None
        if field_name == "trade_terms" and source_kind == "current_body":
            for line in re.finditer(r"^[^\r\n]+$", text, re.M):
                phrases = list(
                    re.finditer(
                        rf"\b(?:on|under|in)[ \t]+(?P<term>{_INCOTERM.pattern})[ \t]+"
                        r"(?P<role>terms?|conditions?)\b",
                        line.group(),
                        re.I,
                    )
                )
                if _TERM_OPTIONS.search(line.group()):
                    continue
                if len(phrases) == 1:
                    phrase = phrases[0]
                    if not re.search(
                        r"\b(?:not|never|without)\b", line.group()[: phrase.start()], re.I
                    ) and (
                        not phrase["role"].lower().startswith("condition")
                        or not _ROUTE_QUALIFIER.search(line.group())
                    ):
                        yield line, phrase["term"].upper(), None
                elif not phrases and not _ROUTE_QUALIFIER.search(line.group()):
                    term = _REQUEST_TERM.search(line.group()) or _NEW_CARGO_AIRPORT.search(
                        line.group()
                    )
                    if term and term["term"]:
                        yield line, term["term"].upper(), None
                    elif value := _quoted_request_term(line.group()):
                        yield line, value, None
        if field_name in {"origin", "destination"} and source_kind == "current_body":
            yield from _body_direction(field_name, text)
        if field_name == "destination" and source_kind == "current_body":
            yield from _explicit_body_destination(text)
            pattern = (
                r"(?i:\b(?:rates?|charges?|quote|quotation|airfreight)\b[^\r\n.?!;]{0,80}?\bto\s+)"
                r"(?P<destination>[A-Z]{3}|[A-Z][a-z]+(?:[ \t]+[A-Z][a-z]+){0,2})"
                r"(?=[ \t]*(?:[.!?,;]|$))"
            )
            for line in re.finditer(r"^[^\r\n]+$", text, re.M):
                if re.search(r"\bfrom\b[^\r\n.?!;]*\bto\b", line.group(), re.I):
                    continue
                places = list(re.finditer(pattern, line.group()))
                if len(places) == 1 and not re.search(
                    r"\b(?:not|never|without)\b", line.group()[: places[0].start()], re.I
                ):
                    yield line, places[0].group("destination"), None
        return
    if field_name == "chargeable_weight":
        for match in _CHARGEABLE_WEIGHT.finditer(text):
            if re.search(
                r"(?:not|no|without|未|无|没有|不)\s*$",
                text[max(0, match.start() - 16) : match.start()],
                re.I,
            ):
                continue
            yield (
                match,
                _decimal_value(_kg(match.group("number").replace(",", "."), match.group("unit"))),
                "kg",
            )
        return
    if source_kind == "current_body" and field_name in {"line_gross_weight", "line_volume"}:
        pattern = (
            r"^[ \t]*(?:g\s*\.\s*weight|gross[ \t]+weight)[ \t]*[:：][ \t]*(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>kg|kgs|g|lb|lbs)[ \t]*$"
            if field_name == "line_gross_weight"
            else r"^[ \t]*measures?[ \t]*[:：][ \t]*(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>cbm|m³|m3)[ \t]*$"
        )
        for match in re.finditer(pattern, text, re.I | re.M):
            value = (
                _decimal_value(_kg(match.group("number"), match.group("unit")))
                if field_name == "line_gross_weight"
                else _decimal_value(match.group("number"))
            )
            yield match, value, "kg" if field_name == "line_gross_weight" else "CBM"
        if field_name == "line_volume":
            for match in _BODY_DECIMAL_VOLUME.finditer(text):
                yield (
                    match,
                    _decimal_value((match["label"] or match["value"]).replace(",", ".")),
                    "CBM",
                )
            pattern = (
                r"^[ \t]*\d+\s*(?:cartons?|ctns?)\s*[-–]\s*\d+(?:\.\d+)?\s*kgs?\s*[-–]\s*"
                r"(?P<number>\d+(?:\.\d+)?)\s*(?:cbm|m³|m3)[ \t]*$"
            )
            for match in re.finditer(pattern, text, re.I | re.M):
                yield match, _decimal_value(match.group("number")), "CBM"
    pattern = (
        _COUNT
        if field_name == "ctns"
        else _PACKAGE_TOTAL
        if field_name == "package_count"
        else _VOLUME
        if field_name == "line_volume"
        else _WEIGHT
    )
    matches = list(pattern.finditer(text))
    if field_name == "line_volume":
        for cargo in _CARGO_VOLUME.finditer(text):
            line_end = text.find("\n", cargo.end())
            line = text[cargo.start() : line_end if line_end >= 0 else len(text)]
            number_prefix = text[
                max(cargo.start(), cargo.start("number") - 24) : cargo.start("number")
            ]
            if (
                len(list(_VOLUME_TOKEN.finditer(line))) == 1
                and not _VOLUME.search(cargo.group(0))
                and not re.search(r"\b(?:each|per)\b|每", line, re.I)
                and not re.search(r"(?:not|no|without|未|无|没有|不)\s*$", number_prefix, re.I)
            ):
                matches.append(cargo)
    for match in matches:
        if source_kind == "current_body":
            line_start = text.rfind("\n", 0, match.start()) + 1
            context = text[line_start : match.start()]
            previous = re.search(
                r"\b(?:while|whereas)\s+(?:the\s+)?(?:original|previous|old)\s+"
                r"(?:one|value|data|cargo|shipment)\b",
                context,
                re.I,
            )
            if (
                previous
                and re.search(
                    r"\b(?:new|current|updated|revised)\b", context[: previous.start()], re.I
                )
                and not re.search(r"[;.!?](?:\s|$)", context[previous.end() :])
            ):
                continue
        prefix = text[max(0, match.start() - 16) : match.start()].lower()
        if re.search(r"(?:not|no|without|未|无|没有|不)\s*$", prefix):
            continue
        if field_name in {"ctns", "package_count"}:
            value = Decimal(match.group("number"))
            if value != value.to_integral_value():
                continue
            yield match, _decimal_value(value), None
        elif field_name == "line_volume":
            yield match, _decimal_value(match.group("number")), "CBM"
        else:
            kind = match.group("kind").lower().replace(" ", "").replace(".", "")
            if kind == "g" and match.group("scope").lower() not in {"total", "line", "总", "整行"}:
                continue
            is_gross = kind.startswith("g") or kind == "毛重"
            per_item = match.group("scope").lower().startswith(("each", "per", "每"))
            expected = (
                ("unit_" if per_item else "line_") + ("gross" if is_gross else "net") + "_weight"
            )
            if field_name == expected:
                yield match, _decimal_value(_kg(match.group("number"), match.group("unit"))), "kg"


def _scope_analysis(requirement, text):
    """Bound an explicit object anchor to an unambiguous line and report ambiguity."""
    anchor = requirement.get("object_anchor")
    source_pattern = requirement.get("source_pattern")
    if not anchor:
        if source_pattern and not re.search(source_pattern, text, re.I):
            return [], False
        return [(0, len(text))], False
    anchor_pattern = re.compile(
        r"(?<![A-Za-z0-9_])" + re.escape(anchor) + r"(?![A-Za-z0-9_])", re.I
    )
    family = re.fullmatch(r"([A-Za-z]+)([0-9]+)", anchor)
    label_pattern = (
        re.compile(
            r"(?<![A-Za-z0-9_])" + re.escape(family.group(1)) + r"[0-9]+(?![A-Za-z0-9_])",
            re.I,
        )
        if family
        else None
    )
    spans, offset, ambiguous = [], 0, False
    for line in text.splitlines(keepends=True):
        if anchor_pattern.search(line) and (
            not source_pattern or re.search(source_pattern, line, re.I)
        ):
            if label_pattern is not None and any(
                match.group(0).casefold() != anchor.casefold()
                for match in label_pattern.finditer(line)
            ):
                ambiguous = True
            else:
                spans.append((offset, offset + len(line)))
        offset += len(line)
    return spans, ambiguous


def _scoped_spans(requirement, text):
    return _scope_analysis(requirement, text)[0]


_QUOTED_START = re.compile(
    r"(?im)^\s*(?:on\s+[^\n]+\s+wrote:|[-_]{3,}\s*(?:original message|forwarded message)[-_ ]*|"
    r"[-_]{3,}\s*original\s*[-_]{3,}|"
    r"(?:from|de|发件人)\s*[:：]\s*\S[^\n]*)\s*$"
)
_SPLIT_QUOTE_FROM = re.compile(r"\s*(?:from|de|发件人)\s*[:：]\s*", re.I)
_SPLIT_QUOTE_SENT = re.compile(
    r"^\s*(?:sent|date|enviad[oa]\s+em|enviado(?:\s+el)?|fecha|发送时间|日期)\s*[:：]",
    re.I,
)
_SPLIT_QUOTE_TO = re.compile(r"^\s*(?:to|para|收件人)\s*[:：]", re.I)
_SPLIT_QUOTE_LOOKAHEAD = 12


def _quoted_offset(blocks, index):
    block = blocks[index]
    quote = _QUOTED_START.search(block["text"])
    if quote:
        return quote.start()
    if not block.get("inline_group") or not _SPLIT_QUOTE_FROM.fullmatch(block["text"]):
        return None
    # A bare sender label can be part of a current form. Require an ordered
    # sender, timestamp and recipient cluster, bounded to nearby READ nodes.
    sent = False
    for item in blocks[index + 1 : index + 1 + _SPLIT_QUOTE_LOOKAHEAD]:
        text = item["text"]
        if _SPLIT_QUOTE_FROM.fullmatch(text) or _QUOTED_START.search(text):
            break
        if _SPLIT_QUOTE_SENT.match(text):
            sent = True
        elif _SPLIT_QUOTE_TO.match(text):
            return 0 if sent else None
    return None


def _current_body_blocks(blocks):
    for index, block in enumerate(blocks):
        if "markdown_section" in block:
            if block["markdown_section"] == "current":
                yield block, len(block["text"])
            continue
        quote = _quoted_offset(blocks, index)
        if quote is None:
            yield block, len(block["text"])
        else:
            if quote:
                yield block, quote
            break


def _scoped_blocks(requirement, blocks):
    bounded = (
        _current_body_blocks(blocks)
        if requirement.get("source_kind") == "current_body"
        else ((block, len(block["text"])) for block in blocks)
    )
    for block, boundary in bounded:
        for start, end in _scoped_spans(requirement, block["text"][:boundary]):
            yield block, start, block["text"][start:end]


def _body_table_totals(blocks, field_name):
    """Read paired gross and volume from one explicitly headed total row."""

    def at(cells, row, header):
        col = header["cell"]["start_col_offset_idx"]
        matches = [
            cell
            for cell in cells
            if cell["cell"]["start_row_offset_idx"] == row
            and cell["cell"]["end_row_offset_idx"] == row + 1
            and cell["cell"]["start_col_offset_idx"] == col
            and cell["cell"]["end_col_offset_idx"] == col + 1
        ]
        return matches[0] if len(matches) == 1 else None

    tables = {}
    for block in blocks:
        if block.get("cell") and "/table_cells/" in block["pointer"]:
            tables.setdefault(block["pointer"].split("/table_cells/")[0], []).append(block)
    results = []
    for cells in tables.values():
        header_row = min(cell["cell"]["start_row_offset_idx"] for cell in cells)
        headers = [cell for cell in cells if cell["cell"]["start_row_offset_idx"] == header_row]
        gross_headers = [
            cell for cell in headers if re.fullmatch(r"\s*gross\s+weight\s*", cell["text"], re.I)
        ]
        volume_headers = [
            cell for cell in headers if re.fullmatch(r"\s*(?:cbm|m³|m3)\s*", cell["text"], re.I)
        ]
        markers = [
            cell for cell in cells if re.fullmatch(r"\s*total\s*[:：]?\s*", cell["text"], re.I)
        ]
        if len(gross_headers) != 1 or len(volume_headers) != 1 or len(markers) != 1:
            continue
        gross_header, volume_header, marker = gross_headers[0], volume_headers[0], markers[0]
        row = marker["cell"]["start_row_offset_idx"]
        marker_row = [
            cell
            for cell in cells
            if cell["cell"]["start_row_offset_idx"] == row and cell["text"].strip()
        ]
        if len(marker_row) == 1:
            row += 1

        gross, volume = at(cells, row, gross_header), at(cells, row, volume_header)
        if not gross or not volume:
            continue
        gm = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(kg|kgs|g|lb|lbs)\s*", gross["text"], re.I)
        vm = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(cbm|m³|m3)\s*", volume["text"], re.I)
        if not gm or not vm:
            continue
        if field_name == "line_gross_weight":
            results.append(
                (
                    _decimal_value(_kg(gm.group(1), gm.group(2))),
                    "kg",
                    [gross, gross_header, marker, volume, volume_header],
                )
            )
        else:
            results.append(
                (
                    _decimal_value(vm.group(1)),
                    "CBM",
                    [volume, volume_header, marker, gross, gross_header],
                )
            )
    return results


def _body_form_cell(name, label, value):
    """Bind the tested form labels only to their explicitly adjacent value cell."""
    label, value = label.strip(), value.strip()
    if (
        name == "trade_terms"
        and re.fullmatch(
            r"incoterms?\s*[:：]?\s*\(please click the yellow cell\)",
            label,
            re.I,
        )
        and _INCOTERM.fullmatch(value)
    ):
        return value.upper(), None
    if name == "ctns" and re.fullmatch(r"total (?:number of )?cartons?\s*[:：]?", label, re.I):
        if count := re.fullmatch(r"(\d+(?:\.0+)?)\s*(?:cartons?|ctns?)?", value, re.I):
            return _decimal_value(count[1]), None
    if name == "line_gross_weight" and re.fullmatch(
        r"total\s+(?:gross\s+weight|g\.?\s*w\.?)\s*[:：]?\s*kgs?\s*[:：]?",
        label,
        re.I,
    ):
        if weight := re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:kgs?)?", value, re.I):
            return _decimal_value(weight[1]), "kg"
    return None


def _body_total_fields(blocks, requirement, *, agent_labels=False):
    """Associate an explicit total label and value in one HTML inline group."""
    patterns = {
        "ctns": (
            r"(?:no\.?\s*(?:of\s+)?|total\s+)?(?:cartons?|ctns?)\s*[:：]?",
            r"(\d+)\s*(?:cartons?|ctns?)",
        ),
        "package_count": (
            r"total\s+packages?\s*[:：]",
            r"(\d+)\s*(?:packages?|pallets?|cartons?|ctns?)",
        ),
        "line_gross_weight": (
            r"total\s+gross\s+weight\s*[:：]",
            r"(\d+(?:\.\d+)?)\s*(kg|kgs|g|lb|lbs)",
        ),
        "line_volume": (r"total\s+volume\s*[:：]", r"(\d+(?:\.\d+)?)\s*(cbm|m³|m3)"),
        "chargeable_weight": (
            r"(?:chargeable\s+weight|计费重(?:量)?)\s*[:：]",
            r"(\d+(?:\.\d+|,\d{1,2})?)\s*(kg|kgs|g|lb|lbs)",
        ),
    }
    if agent_labels:
        for name in ("line_gross_weight", "line_volume"):
            label, value = patterns[name]
            patterns[name] = (label.replace(r"total\s+", r"(?:total\s+)?", 1), value)
    name = requirement["field_name"]
    if (
        requirement.get("source_kind") != "current_body"
        or name not in {*patterns, *_INQUIRY_LABELS}
        or requirement.get("object_anchor")
    ):
        return []
    current = [block for block, end in _current_body_blocks(blocks) if end == len(block["text"])]
    label_pattern, value_pattern = patterns.get(
        name, (rf"(?:{_INQUIRY_LABELS.get(name, '')})[ \t]*[:：]", None)
    )
    results = (
        _body_table_totals(current, name) if name in {"line_gross_weight", "line_volume"} else []
    )
    for index, label in enumerate(current[:-1]):
        value = current[index + 1]
        group = label.get("inline_group")
        inline_pair = group and group == value.get("inline_group")
        left, right = label.get("cell"), value.get("cell")
        table_pair = (
            left
            and right
            and "/table_cells/" in label["pointer"]
            and label["pointer"].split("/table_cells/")[0]
            == value["pointer"].split("/table_cells/")[0]
            and left["start_row_offset_idx"] is not None
            and left["end_col_offset_idx"] is not None
            and left["start_row_offset_idx"] == right["start_row_offset_idx"]
            and left["end_row_offset_idx"] == right["end_row_offset_idx"]
            and left["end_col_offset_idx"] == right["start_col_offset_idx"]
        )
        if requirement.get("source_pattern") and not re.search(
            requirement["source_pattern"], label["text"], re.I
        ):
            continue
        if name in _LOCATION_LABELS:
            typed = re.fullmatch(
                rf"\s*(?P<label>{_LOCATION_LABELS[name]})[ \t]*[:：,，]?[ \t]*", label["text"], re.I
            )
            if typed:
                standalone_address = (
                    not left
                    and not right
                    and name == "pickup_city"
                    and re.fullmatch(_PICKUP_ADDRESS_LABEL, typed["label"], re.I)
                    and re.match(r"\s*(?:add(?:ress)?|地址)[ \t]*[:：]", value["text"], re.I)
                )
                if table_pair or inline_pair or standalone_address:
                    if location := _typed_location_value(name, typed["label"], value["text"]):
                        results.append((location, None, [value, label]))
            continue
        if name in {"destination", "trade_terms"} and not left and not right:
            label_ref = re.fullmatch(r"/texts/(\d+)/text", label["pointer"])
            value_ref = re.fullmatch(r"/texts/(\d+)/text", value["pointer"])
            if (
                label_ref
                and value_ref
                and int(value_ref[1]) == int(label_ref[1]) + 1
                and (observed := _adjacent_inquiry_value(name, label["text"], value["text"]))
            ):
                results.append((observed, None, [value, label]))
                continue
        if (
            name == "destination"
            and not left
            and not right
            and (route := re.fullmatch(r"[ \t]*(?i:to)[ \t]+([A-Z]{3})[ \t]*", label["text"]))
            and _SLASH_CARGO_SUMMARY.fullmatch(value["text"])
        ):
            results.append((route[1], None, [label, value]))
            continue
        if table_pair and (proof := _body_form_cell(name, label["text"], value["text"])):
            results.append((*proof, [value, label]))
            continue
        local_label, local_value = label_pattern, value_pattern
        if table_pair and name in {"destination", "origin", "trade_terms", "cargo_ready_time"}:
            local_label = rf"(?:{_INQUIRY_LABELS[name]})\s*[:：]?"
        elif table_pair and name == "line_gross_weight":
            local_label = r"(?:total\s+)?gross\s+weight\s*[:：]?"
        elif table_pair and name == "line_volume":
            local_label, local_value = (
                r"(?:total\s+)?cbm\s*[:：]?",
                r"(\d+(?:\.\d+)?)\s*(?:cbm|m³|m3)?",
            )
        elif not inline_pair and not (table_pair and name == "ctns"):
            continue
        if not re.fullmatch(local_label, label["text"].strip(), re.I):
            continue
        if name in _INQUIRY_LABELS:
            results.extend(
                (item, None, [value, label]) for item in _inquiry_values(name, value["text"])
            )
            continue
        match = re.fullmatch(local_value, value["text"].strip(), re.I)
        if not match:
            continue
        number_text = match.group(1).replace(",", ".")
        number = _decimal_value(number_text)
        unit = None
        if name in {"line_gross_weight", "chargeable_weight"}:
            number, unit = _decimal_value(_kg(number_text, match.group(2))), "kg"
        elif name == "line_volume":
            unit = "CBM"
        results.append((number, unit, [value, label]))
    return results


def _section(requirement, blocks, target):
    markers = requirement.get("quoted_section_markers")
    if not markers:
        return None
    if (
        not isinstance(markers, list)
        or len(markers) < 3
        or not all(isinstance(x, str) and x for x in markers)
    ):
        raise ValueError("Quoted section requires explicit header markers")
    target_index = blocks.index(target)
    previous = blocks[max(0, target_index - 30) : target_index]
    found = []
    position = 0
    for marker in markers:
        match = next(
            (
                index
                for index in range(position, len(previous))
                if previous[index]["text"] == marker
            ),
            None,
        )
        if match is None:
            raise ValueError("Quoted section markers are not present before the candidate")
        found.append({"json_pointer": previous[match]["pointer"], "excerpt": marker})
        position = match + 1
    return {
        "kind": "quoted_body",
        "quote": markers[1],
        "markers": found,
        "basis": "caller-confirmed embedded-message headers in the READ artifact",
    }


def _derive_unit(
    requirement,
    selected_blocks,
    candidates,
    sources,
    bindings,
    material,
    execution,
    content_root,
    record,
):
    if requirement["field_name"] != "unit_gross_weight":
        return None
    evaluated = evaluate_candidates(candidates, requirement["object_selector"])
    if evaluated is None:
        return None
    parents = evaluated["parents"]
    for block, offset, text in _scoped_blocks(requirement, selected_blocks):
        match = _EQUAL.search(text)
        if not match or re.search(
            r"(?:没有|未|不|not|no)\s*(?:声明|证明|confirmed)?\s*$",
            text[max(0, match.start() - 16) : match.start()],
            re.I,
        ):
            continue
        equality = _source(
            sources,
            bindings,
            material,
            execution,
            block,
            offset + match.start(),
            offset + match.end(),
            content_root,
            record,
            source_kind=requirement["source_kind"],
            section=_section(requirement, selected_blocks, block),
        )
        source_ids = list(
            dict.fromkeys(
                parents["ctns"]["field"]["source_ids"]
                + parents["line_gross_weight"]["field"]["source_ids"]
                + [equality]
            )
        )
        return {
            "candidate_id": f"C{len(candidates) + 1}",
            "field_name": "unit_gross_weight",
            "field_type": "kg_field",
            "object_id": requirement["object_selector"],
            "quantity_scope": "per_item",
            "quantity_kind": "gross_weight",
            "packaging_level": "outer_carton",
            "uniform_per_item_evidence": equality,
            "derived_from": [
                parents["ctns"]["candidate_id"],
                parents["line_gross_weight"]["candidate_id"],
            ],
            "derivation": "line_gross_weight / ctns; explicit equal-weight evidence",
            "field": {
                "value": evaluated["value"],
                "unit": "kg",
                "confidence": None,
                "status": "known",
                "origin": "calculated",
                "rule_id": "equal-carton-weights-v1",
                "input_paths": [
                    f"/candidates/{candidates.index(parents['ctns'])}/field",
                    f"/candidates/{candidates.index(parents['line_gross_weight'])}/field",
                ],
                "source_ids": source_ids,
                "raw_values": [],
            },
        }
    return None


def _is_named_inline_pdf(record):
    metadata = record.get("metadata") or {}
    filename = metadata.get("mime_filename")
    return (
        record.get("kind") == "inline"
        and record.get("format") == "pdf"
        and metadata.get("disposition") == "inline"
        and isinstance(filename, str)
        and bool(filename.strip())
        and filename == record.get("filename")
    )


def _source(
    sources,
    bindings,
    material,
    execution,
    block,
    start,
    end,
    content_root,
    record,
    *,
    source_kind,
    section=None,
):
    source_id = f"S{len(sources) + 1}"
    excerpt = block["text"][start:end]
    locator = {"json_pointer": block["pointer"], "char_start": start, "char_end": end}
    provenance = block.get("provenance", [])
    if len(provenance) == 1 and provenance[0].get("page_no"):
        locator["page"] = provenance[0]["page_no"]
    if material["kind"] == "body" and source_kind == "quoted_body" and section:
        kind = "quoted_body"
    elif (
        material["kind"] == "body"
        and source_kind == "current_body"
        and material["source_role"] == "current"
    ):
        kind = "current_body"
    elif source_kind == "attachment" and (
        material["kind"] == "attachment" or _is_named_inline_pdf(record)
    ):
        # The evidence schema uses "attachment" for file sources; the material keeps
        # its original inline disposition, CID and MIME location in the manifest.
        kind = "attachment"
    else:
        raise ValueError("Source section kind is not established for this material")
    sources.append(
        {
            "source_id": source_id,
            "email_ref_id": material["email_ref_id"],
            "kind": kind,
            "material_id": material["material_id"],
            "excerpt": excerpt,
            "locator": locator,
            "location_status": "exact",
        }
    )
    representation_locator = None
    descriptor = record.get("content")
    if descriptor and record.get("format") in {"text", "html", "markdown"}:
        root = Path(content_root).resolve(strict=True)
        path = (root / descriptor["path"]).resolve(strict=True)
        if not path.is_relative_to(root):
            raise ValueError("Representation path escapes content root")
        raw = path.read_bytes()
        if _sha(raw) != descriptor["sha256"]:
            raise ValueError("Representation SHA-256 mismatch")
        text = raw.decode("utf-8-sig")
        offset = text.find(excerpt)
        if offset >= 0 and text.find(excerpt, offset + 1) < 0:
            representation_locator = {"char_start": offset, "char_end": offset + len(excerpt)}
    bindings.append(
        {
            "source_id": source_id,
            "material_id": material["material_id"],
            "material_revision": 1,
            "content_hash": material["original_sha256"],
            "artifact_id": execution["execution_id"],
            "artifact_hash": execution["artifact_hash"],
            "text_version": execution["artifact_hash"],
            "representation_hash": material["representation_sha256"],
            "representation_revision": 1,
            "locator_basis": "artifact",
            "coordinate_system": "unicode_codepoint_half_open",
            "character_unit": "unicode_codepoint",
            "mapping_status": "exact_unique_substring"
            if representation_locator
            else "artifact_only",
            "origin_locator": None,
            "representation_locator": representation_locator,
            "docling_provenance": provenance,
            "docling_cell": block.get("cell"),
            "section_evidence": section,
        }
    )
    return source_id


_STAGE_OWNERS = {
    "acquisition": "preprocessing",
    "parsing": "document_reader",
    "extraction": "field_extractor",
    "aggregation": "rule_validator",
}


def _task_scope(scope, manifest, profile):
    """Check a caller-owned scope against the complete input inventory."""
    if not isinstance(scope, dict):
        raise ValueError("An independent task scope is required")
    if (
        scope.get("task_id") != manifest["task_id"]
        or scope.get("email_ref_id") != manifest["emails"][0]["email_ref_id"]
        or not scope.get("scope_ref")
        or (profile is not None and profile["task_scope_ref"] != scope["scope_ref"])
    ):
        raise ValueError("Task scope identity or profile binding mismatch")
    ids = [item["material_id"] for item in manifest["materials"]]
    if len(set(ids)) != len(ids) or scope.get("material_ids") != ids:
        raise ValueError("Task scope must contain the full independent material inventory")
    if scope.get("required_stages") != list(_STAGE_OWNERS):
        raise ValueError("All four local stages must be requested")
    if scope.get("original_requirement") != "explicit_representation_only":
        raise ValueError("Only explicitly representation-scoped output is supported")
    stages = scope.get("expected_scopes")
    if not isinstance(stages, dict) or set(stages) != set(_STAGE_OWNERS):
        raise ValueError("Independent expected scopes are required for all stages")
    expected = [mid + "/representation" for mid in ids]
    for name, owner in _STAGE_OWNERS.items():
        stage = stages[name]
        if (
            not isinstance(stage, dict)
            or stage.get("scope_ref") != f"{scope['task_id']}:{name}"
            or stage.get("scope_owner") != owner
            or stage.get("expected_refs") != expected
            or not isinstance(stage.get("unknown_scope"), bool)
        ):
            raise ValueError("Independent expected scope is invalid or incomplete")
    return copy.deepcopy(scope)


def _coverage(task_id, inventory, executions, profile, task_scope=None):
    by_execution = {item["execution_id"]: item for item in executions if item.get("execution_id")}
    expected, processed, unread = [], [], []
    observed = []
    for item in inventory:
        scope = item["material_id"] + "/representation"
        expected.append(scope)
        execution = by_execution.get(item.get("execution_id"))
        if (
            execution
            and execution["execution_state"] in {"success", "partial_success"}
            and execution.get("ref")
        ):
            result = execution["result"]
            if (
                execution["executed_profile"]["reader"] == "TextReader"
                and execution["execution_state"] == "success"
            ):
                processed.append(scope)
                execution["coverage"]["actual_scope"] = {
                    "location": result["location"],
                    "original_coverage": "unknown",
                }
            else:
                unread.append(scope)
                execution["coverage"]["actual_scope"] = {
                    "observed_docling_pages": result.get("document_pages"),
                    "observed_text_items": result.get("text_items"),
                    "observed_table_items": result.get("table_items"),
                    "representation_completeness": "unverified",
                }
                if result.get("read_strategy") == "adaptive_pdf":
                    execution["coverage"]["actual_scope"].update(
                        unread_pages=result.get("unread_pages", []),
                        pass_states=[
                            {
                                "kind": item["kind"],
                                "pages": item["pages"],
                                "status": item["status"],
                                "reason": item.get("reason") or item.get("termination_reason"),
                            }
                            for item in result.get("passes", [])
                        ],
                    )
            observed.append(
                {
                    "material_id": item["material_id"],
                    "execution_id": item["execution_id"],
                    "actual_scope": execution["coverage"]["actual_scope"],
                }
            )
        else:
            unread.append(scope)
    acquired = [
        item["material_id"] + "/representation"
        for item in inventory
        if item["representation_sha256"]
    ]
    coverage = {
        "acquisition": {
            "state": "partial",
            "expected_refs": expected.copy(),
            "processed_refs": acquired,
            "unread_refs": [ref for ref in expected if ref not in acquired],
            "exempt_refs": [],
            "unknown_scope": True,
            "requested": True,
            "stage_state": "finished",
            "scope_ref": f"{task_id}:acquisition",
            "scope_owner": "preprocessing",
        }
    }
    target_ids = {r["material_id"] for r in profile["requirements"]} if profile else set()
    timed_out = any(
        item.get("result", {}).get("termination_reason") == "timeout" for item in executions
    )
    inventory_by_id = {item["material_id"]: item for item in inventory}
    checked_blocks = {}
    ambiguous_scopes = set()
    for requirement in profile["requirements"] if profile else []:
        item = inventory_by_id[requirement["material_id"]]
        execution = by_execution.get(item.get("execution_id"))
        if (
            not execution
            or execution["execution_state"] not in {"success", "partial_success"}
            or not execution.get("ref")
        ):
            continue
        eid = execution["execution_id"]
        if eid not in checked_blocks:
            checked_blocks[eid] = _blocks(execution)[1]
        if any(_scope_analysis(requirement, block["text"])[1] for block in checked_blocks[eid]):
            ambiguous_scopes.add(requirement["material_id"] + "/representation")
    for stage, owner in (
        ("parsing", "document_reader"),
        ("extraction", "field_extractor"),
        ("aggregation", "rule_validator"),
    ):
        stage_processed = (
            processed
            if stage == "parsing"
            else [
                ref
                for ref in processed
                if ref.rsplit("/", 1)[0] in target_ids and ref not in ambiguous_scopes
            ]
        )
        coverage[stage] = {
            "state": "partial",
            "expected_refs": expected.copy(),
            "processed_refs": stage_processed.copy(),
            "unread_refs": [ref for ref in expected if ref not in stage_processed],
            "exempt_refs": [],
            "unknown_scope": True,
            "requested": True,
            "stage_state": "not_started"
            if stage != "parsing" and profile is None
            else "interrupted"
            if stage == "parsing" and timed_out
            else "finished",
            "scope_ref": f"{task_id}:{stage}",
            "scope_owner": owner,
        }
    if task_scope is not None:
        for name, stage in coverage.items():
            expected_scope = task_scope["expected_scopes"][name]
            stage["unknown_scope"] = expected_scope["unknown_scope"]
            if (
                not stage["unknown_scope"]
                and not stage["unread_refs"]
                and stage["stage_state"] == "finished"
            ):
                stage["state"] = "complete"
    return coverage, observed


def _complete(coverage, assessments, profile):
    if any(stage["state"] != "complete" for stage in coverage.values()):
        return False
    by_id = {item["requirement_id"]: item for item in assessments}
    for requirement in profile["requirements"]:
        assessment = by_id[requirement["requirement_id"]]
        if not requirement["critical"]:
            continue
        state = assessment["state"]
        if state == "checked_present":
            continue
        if state == "checked_absent" and requirement["missing_policy"] == "allow":
            continue
        if state == "not_applicable" and requirement.get("allow_not_applicable"):
            continue
        return False
    return True


def _inline_company_supported(
    candidate, sources, bindings, executions, profile, manifest, content_root
):
    """Recheck the current-body, sender-domain and OCR bindings for a company candidate."""
    if (
        candidate["field_name"] != "customer_company"
        or candidate["field"]["status"] != "known"
        or not isinstance(candidate["field"]["value"], str)
    ):
        return False
    requirements = [
        r
        for r in profile["requirements"]
        if r["field_name"] == "customer_company"
        and r["object_selector"] == candidate["object_id"]
        and r["source_kind"] == "current_body"
    ]
    if len(requirements) != 1:
        return False
    records = {item["material_id"]: item for item in manifest["materials"]}
    target = records[requirements[0]["material_id"]]
    domain = sender_domain(manifest, target)
    if not domain:
        return False
    images = {
        item["material_id"]: item for item in eligible_inline_images(manifest, target, content_root)
    }
    by_material = {}
    for sid in candidate["field"]["source_ids"]:
        source, binding = sources[sid], bindings[sid]
        mid = source["material_id"]
        if source["kind"] != "signature" or mid not in images:
            return False
        if binding.get("section_evidence") != signature_section_evidence(
            manifest, target, images[mid]
        ):
            return False
        by_material.setdefault(mid, []).append((source, binding))
    if not by_material:
        return False

    def proof(item):
        index, line = item[:2]
        start, end = (item[2], item[3]) if len(item) > 2 else (0, len(line["text"]))
        return (f"/parts/0/lines/{index}/text", start, end, tuple(_box(line)))

    for mid, entries in by_material.items():
        execution_ids = {binding["artifact_id"] for _, binding in entries}
        if len(execution_ids) != 1:
            return False
        execution = executions.get(next(iter(execution_ids)))
        if not execution or execution["executed_profile"]["reader"] != "RapidOCR":
            return False
        if not any(
            item["material_id"] == mid and item.get("intake_source_bound") is True
            for item in execution["occurrence_bindings"]
        ):
            return False
        image_read = json.loads(Path(execution["ref"]).read_bytes())
        if (
            image_read.get("material_id") != mid
            or image_read.get("input_sha256") != images[mid]["content"]["sha256"]
        ):
            return False
        observed = {
            (
                s["locator"]["json_pointer"],
                s["locator"]["char_start"],
                s["locator"]["char_end"],
                tuple(s["locator"].get("region", [])),
            )
            for s, _ in entries
        }
        proofs = []
        for value, brand, support in _company_lines(
            image_read, domain, images[mid].get("scope") or {}
        ):
            if value.casefold() == candidate["field"]["value"].casefold():
                proofs.append({proof(item) for item in (brand, support) if item is not None})
        supported = set().union(*(items for items in proofs if items.issubset(observed)))
        if not supported or supported != observed:
            return False
    return True


def _assess(handoff, profile, read, manifest, content_root, field_contract_ref, task_scope=None):
    """Minimal OUT-CONSUME checks; never recompute the business rules here."""
    if handoff["required_field_profile"] != profile:
        raise ValueError("Independent field profile changed")
    if (
        handoff["field_contract_ref"]["version"] != field_contract_ref["version"]
        or handoff["field_contract_ref"]["sha256"] != field_contract_ref["sha256"]
        or _sha(Path(field_contract_ref["ref"]).read_bytes()) != field_contract_ref["sha256"]
    ):
        raise ValueError("Field contract snapshot hash mismatch")
    if _sha(Path(read["manifest_path"]).read_bytes()) != read["manifest_sha256"]:
        raise ValueError("Manifest changed during result preparation")
    if (
        handoff["handoff_spec_version"] != "0.3.0-draft"
        or handoff["result_scope"] != "material_handoff"
        or handoff["task_id"] != manifest["task_id"]
        or read["task_id"] != manifest["task_id"]
        or handoff["email_ref_id"] != manifest["emails"][0]["email_ref_id"]
        or handoff["input_manifest_ref"]
        != {"ref": read["manifest_path"], "sha256": read["manifest_sha256"]}
        or not read["intake_bound"]
    ):
        raise ValueError("Handoff task, contract or manifest binding mismatch")
    material_ids = {item["material_id"] for item in manifest["materials"]}
    if (
        len(material_ids) != len(manifest["materials"])
        or len(handoff["material_versions"]) != len(material_ids)
        or {item["material_id"] for item in handoff["material_versions"]} != material_ids
        or len(read["inventory"]) != len(material_ids)
        or {item["material_id"] for item in read["inventory"]} != material_ids
    ):
        raise ValueError("Material inventory does not match manifest")
    artifacts = {item["artifact_id"]: item for item in handoff["artifact_refs"]}
    source_ids = set()
    sources = {item["source_id"]: item for item in handoff["sources"]}
    bindings = {item["source_id"]: item for item in handoff["source_bindings"]}
    if (
        len(artifacts) != len(handoff["artifact_refs"])
        or len(sources) != len(handoff["sources"])
        or len({b["source_id"] for b in handoff["source_bindings"]})
        != len(handoff["source_bindings"])
    ):
        raise ValueError("Duplicate artifact or source identity")
    records = {item["material_id"]: item for item in manifest["materials"]}
    material_versions = {item["material_id"]: item for item in handoff["material_versions"]}
    for mid, material in material_versions.items():
        original = records[mid].get("original")
        if material["content_hash"] != (original["sha256"] if original else None):
            raise ValueError("Material original hash does not match the manifest")
        if bool(material["raw_ref"]) != bool(original):
            raise ValueError("Material original availability does not match the manifest")
        if (
            original
            and Path(material["raw_ref"]).resolve()
            != (Path(content_root) / original["path"]).resolve()
        ):
            raise ValueError("Material original reference does not match the manifest")
        representation = records[mid].get("content")
        if bool(material["representation_ref"]) != bool(representation) or (
            representation and material["representation_ref"]["sha256"] != representation["sha256"]
        ):
            raise ValueError("Material representation does not match the manifest")
        if (
            representation
            and Path(material["representation_ref"]["ref"]).resolve()
            != (Path(content_root) / representation["path"]).resolve()
        ):
            raise ValueError("Material representation reference does not match the manifest")
        if material["availability"] != ("available" if representation else "missing"):
            raise ValueError("Material availability does not match the manifest")
        if material["original_status"] != ("available" if original else "unavailable"):
            raise ValueError("Material original status does not match the manifest")
        record = records[mid]
        expected_role = "supplement" if record["role"] == "supplementary" else record["role"]
        if (
            material["source_email_ref_id"] != record["email_ref_id"]
            or material["source_role"] != expected_role
            or material["role_raw"] != record["role"]
            or material["kind"] != record["kind"]
            or material["representation"] != record["representation"]
            or material["parent_material_id"] != record["parent_material_id"]
        ):
            raise ValueError("Material occurrence does not match the manifest")
    executions = {
        item["execution_id"]: item for item in read["executions"] if item.get("execution_id")
    }
    current_body_ends = {}
    for artifact in artifacts.values():
        if (
            not artifact["material_ids"]
            or len(artifact["material_ids"]) != len(set(artifact["material_ids"]))
            or any(
                material_by_id not in records
                or not records[material_by_id].get("content")
                or records[material_by_id]["content"]["sha256"]
                != artifact["input_representation_hash"]
                for material_by_id in artifact["material_ids"]
            )
        ):
            raise ValueError("Artifact representation binding mismatch")
        parsed_raw = Path(artifact["parsed_artifact_ref"]).read_bytes()
        if _sha(parsed_raw) != artifact["parsed_artifact_sha256"]:
            raise ValueError("Parsed artifact execution reference hash mismatch")
        if _sha(Path(artifact["ref"]).read_bytes()) != artifact["artifact_hash"]:
            raise ValueError("READ artifact hash mismatch")
        parsed = json.loads(parsed_raw)
        execution = executions[artifact["execution_id"]]
        if (
            parsed["artifact_hash"] != artifact["artifact_hash"]
            or parsed["input_plan_id"] != execution["input_plan_id"]
            or parsed["input_ref"] != execution["input_ref"]
            or parsed["ref"] != execution["ref"]
            or {item["material_id"] for item in parsed["occurrence_bindings"]}
            != set(artifact["material_ids"])
        ):
            raise ValueError("Parsed artifact execution binding mismatch")
        if (
            execution["executed_profile"]["reader"] == "TextReader"
            and execution["execution_state"] == "success"
        ):
            record = records[artifact["material_ids"][0]]
            root = Path(content_root).resolve(strict=True)
            content_path = (root / record["content"]["path"]).resolve(strict=True)
            if not content_path.is_relative_to(root):
                raise ValueError("READ representation escapes content root")
            representation = content_path.read_bytes().decode("utf-8-sig")
            document = json.loads(Path(artifact["ref"]).read_bytes())
            expected_location = {
                "basis": "decoded_representation_unicode_codepoints",
                "start": 0,
                "end": len(representation),
            }
            if (
                document.get("text") != representation
                or execution["result"].get("location") != expected_location
            ):
                raise ValueError("TextReader did not read the complete declared representation")
        for occurrence in parsed["occurrence_bindings"]:
            if (
                occurrence["material_revision"]
                != material_versions[occurrence["material_id"]]["material_revision"]
            ):
                raise ValueError("Parsed artifact occurrence revision mismatch")
    for binding in handoff["source_bindings"]:
        source = sources[binding["source_id"]]
        artifact = artifacts[binding["artifact_id"]]
        raw = Path(artifact["ref"]).read_bytes()
        if (
            _sha(raw) != artifact["artifact_hash"]
            or binding["artifact_hash"] != artifact["artifact_hash"]
        ):
            raise ValueError("Evidence artifact hash mismatch")
        if (
            source["material_id"] != binding["material_id"]
            or source["material_id"] not in artifact["material_ids"]
        ):
            raise ValueError("Evidence material binding mismatch")
        material = material_versions[source["material_id"]]
        if (
            source["email_ref_id"] != material["source_email_ref_id"]
            or binding["material_revision"] != material["material_revision"]
            or binding["representation_revision"] != material["representation_revision"]
            or binding["text_version"] != artifact["artifact_hash"]
            or binding["locator_basis"] != "artifact"
            or binding["coordinate_system"] != "unicode_codepoint_half_open"
            or binding["character_unit"] != "unicode_codepoint"
            or source["location_status"] != "exact"
        ):
            raise ValueError("Evidence email or occurrence revision mismatch")
        if binding["origin_locator"] is not None:
            raise ValueError("Original evidence locator is not supported by this local reader")
        text = _pointer(json.loads(raw), source["locator"]["json_pointer"])
        loc = source["locator"]
        if (
            not 0 <= loc["char_start"] < loc["char_end"] <= len(text)
            or text[loc["char_start"] : loc["char_end"]] != source["excerpt"]
        ):
            raise ValueError("Evidence excerpt does not match READ artifact")
        if source["kind"] == "quoted_body":
            section = binding.get("section_evidence")
            if (
                not section
                or section.get("kind") != "quoted_body"
                or len(section.get("markers", [])) < 3
            ):
                raise ValueError("Quoted source lacks section evidence")
            document = json.loads(raw)
            marker_positions = []
            for marker in section["markers"]:
                if _pointer(document, marker["json_pointer"]) != marker["excerpt"]:
                    raise ValueError("Quoted section marker does not match READ artifact")
                marker_positions.append(int(marker["json_pointer"].split("/")[2]))
            target_parts = loc["json_pointer"].split("/")
            if (
                target_parts[1] != "texts"
                or marker_positions != sorted(marker_positions)
                or marker_positions[-1] >= int(target_parts[2])
                or int(target_parts[2]) - marker_positions[0] > 30
            ):
                raise ValueError("Quoted section markers do not locate this excerpt")
        record = records[source["material_id"]]
        if source["kind"] == "current_body":
            key = (binding["artifact_id"], source["material_id"])
            if key not in current_body_ends:
                current_body_ends[key] = {
                    block["pointer"]: end
                    for block, end in _current_body_blocks(
                        _markdown_blocks(
                            executions[binding["artifact_id"]], record, records, content_root
                        )
                    )
                }
            if loc["char_end"] > current_body_ends[key].get(loc["json_pointer"], 0):
                raise ValueError("Evidence is not established as current body")
        if (
            source["kind"] == "current_body"
            and (record["kind"] != "body" or record["role"] != "current")
            or source["kind"] == "quoted_body"
            and record["kind"] != "body"
            or source["kind"] == "attachment"
            and not (record["kind"] == "attachment" or _is_named_inline_pdf(record))
        ):
            raise ValueError("Source kind is inconsistent with the material occurrence")
        if binding["representation_hash"] != record["content"]["sha256"]:
            raise ValueError("Evidence representation mismatch")
        if binding["content_hash"] != material_versions[source["material_id"]]["content_hash"]:
            raise ValueError("Evidence original identity mismatch")
        location = binding["representation_locator"]
        if location is not None:
            root = Path(content_root).resolve(strict=True)
            path = (root / record["content"]["path"]).resolve(strict=True)
            if not path.is_relative_to(root):
                raise ValueError("Evidence representation escapes content root")
            original = path.read_bytes()
            if _sha(original) != binding["representation_hash"]:
                raise ValueError("Evidence representation hash mismatch")
            decoded = original.decode("utf-8-sig")
            if (
                binding["mapping_status"] != "exact_unique_substring"
                or not 0 <= location["char_start"] < location["char_end"] <= len(decoded)
                or decoded[location["char_start"] : location["char_end"]] != source["excerpt"]
                or decoded.find(source["excerpt"]) != location["char_start"]
                or decoded.find(source["excerpt"], location["char_start"] + 1) != -1
            ):
                raise ValueError("Evidence representation location mismatch")
        elif binding["mapping_status"] != "artifact_only":
            raise ValueError("Evidence mapping status does not match its locator")
        source_ids.add(binding["source_id"])
    if source_ids != set(sources):
        raise ValueError("Evidence source binding is incomplete")
    candidate_ids = {candidate["candidate_id"] for candidate in handoff["candidates"]}
    if len(candidate_ids) != len(handoff["candidates"]):
        raise ValueError("Duplicate candidate identity")
    candidate_by_id = {candidate["candidate_id"]: candidate for candidate in handoff["candidates"]}
    candidate_indexes = {
        candidate["candidate_id"]: index for index, candidate in enumerate(handoff["candidates"])
    }
    signature_candidates = {}
    signature_sources = set()
    for candidate in handoff["candidates"]:
        field = candidate["field"]
        if field["status"] not in {"known", "missing", "uncertain", "conflict", "not_applicable"}:
            raise ValueError("Unknown field status")
        expected_type = _field_type(candidate["field_name"])
        value = field["value"]
        confidence = field["confidence"]
        invalid_value = False
        if value is not None:
            if expected_type in {"text_field", "package_field"}:
                invalid_value = not isinstance(value, str) or not value.strip()
                if expected_type == "package_field" and value not in {
                    "carton",
                    "pallet",
                    "wooden_case",
                    "other",
                }:
                    invalid_value = True
            else:
                invalid_value = (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or value < 0
                    or (expected_type == "integer_field" and not isinstance(value, int))
                )
        if (
            candidate["field_name"] not in _FIELDS
            or candidate["field_type"] != expected_type
            or field["origin"] not in {"extracted", "calculated", "default", "unknown"}
            or len(field["source_ids"]) != len(set(field["source_ids"]))
            or invalid_value
            or (
                confidence is not None
                and (
                    isinstance(confidence, bool)
                    or not isinstance(confidence, (int, float))
                    or not math.isfinite(confidence)
                    or not 0 <= confidence <= 1
                )
            )
        ):
            raise ValueError("Candidate field type, origin or value is invalid")
        if (candidate["quantity_scope"], candidate["quantity_kind"]) != _FIELD_SEMANTICS[
            candidate["field_name"]
        ]:
            raise ValueError("Candidate quantity semantics do not match the field")
        if candidate["packaging_level"] not in {"outer_carton", "unknown"} or (
            candidate["field_name"] == "ctns"
            and field["status"] == "known"
            and candidate["packaging_level"] != "outer_carton"
        ):
            raise ValueError("Candidate packaging level is not supported by its field evidence")
        if not set(field["source_ids"]).issubset(source_ids):
            raise ValueError("Candidate references an unknown source")
        has_signature = any(sources[sid]["kind"] == "signature" for sid in field["source_ids"])
        signature_ok = has_signature and _inline_company_supported(
            candidate, sources, bindings, executions, profile, manifest, content_root
        )
        if has_signature and not signature_ok:
            raise ValueError("Inline company evidence is not bound to the current sender and image")
        signature_candidates[candidate["candidate_id"]] = signature_ok
        if signature_ok:
            signature_sources.update(field["source_ids"])
        if (
            any(
                not candidate["object_id"].startswith(sources[sid]["material_id"] + "/")
                for sid in field["source_ids"]
            )
            and not signature_ok
        ):
            raise ValueError("Candidate evidence does not belong to its material")
        if field["status"] == "known" and (field["value"] is None or not field["source_ids"]):
            raise ValueError("Known candidate lacks value or evidence")
        if field["status"] == "known" and field["origin"] == "unknown":
            raise ValueError("Known candidate has unknown origin")
        if field["status"] != "known" and field["value"] is not None:
            raise ValueError("Uncertain candidate cannot expose an effective value")
        if field["origin"] == "calculated":
            if (
                candidate["field_name"] != "unit_gross_weight"
                or field.get("rule_id") != "equal-carton-weights-v1"
                or candidate.get("uniform_per_item_evidence") not in field["source_ids"]
            ):
                raise ValueError("Calculated candidate lacks rule, input or equal-weight evidence")
            parent_ids = candidate.get("derived_from")
            if (
                not isinstance(parent_ids, list)
                or len(parent_ids) != 2
                or len(set(parent_ids)) != 2
                or not set(parent_ids).issubset(candidate_ids)
            ):
                raise ValueError("Calculated candidate has invalid parent identities")
            parents = [candidate_by_id[parent_id] for parent_id in parent_ids]
            if (
                [parent["field_name"] for parent in parents] != ["ctns", "line_gross_weight"]
                or any(parent["object_id"] != candidate["object_id"] for parent in parents)
                or field.get("input_paths")
                != [f"/candidates/{candidate_indexes[parent_id]}/field" for parent_id in parent_ids]
            ):
                raise ValueError("Calculated candidate has inconsistent rule dependencies")
            equality_id = candidate["uniform_per_item_evidence"]
            expected_sources = {
                source_id for parent in parents for source_id in parent["field"]["source_ids"]
            } | {equality_id}
            if set(field["source_ids"]) != expected_sources:
                raise ValueError("Calculated candidate has inconsistent rule evidence")
            equality_source = sources[equality_id]
            equality_locator = equality_source["locator"]
            equality_artifact = artifacts[bindings[equality_id]["artifact_id"]]
            equality_document = json.loads(Path(equality_artifact["ref"]).read_bytes())
            equality_text = _pointer(equality_document, equality_locator["json_pointer"])
            equality_prefix = equality_text[
                max(0, equality_locator["char_start"] - 16) : equality_locator["char_start"]
            ]
            if not _EQUAL.fullmatch(equality_source["excerpt"]) or re.search(
                r"(?:没有|未|不|not|no)\s*(?:声明|证明|confirmed)?\s*$", equality_prefix, re.I
            ):
                raise ValueError("Calculated candidate lacks positive equal-weight evidence")
            if field["status"] == "known":
                evaluated = evaluate_candidates(handoff["candidates"], candidate["object_id"])
                if (
                    evaluated is None
                    or field["value"] != evaluated["value"]
                    or parent_ids
                    != [
                        evaluated["parents"][name]["candidate_id"]
                        for name in ("ctns", "line_gross_weight")
                    ]
                ):
                    raise ValueError("Calculated candidate disagrees with its rule inputs")
        if field["origin"] == "extracted":
            excerpts = [sources[sid]["excerpt"] for sid in field["source_ids"]]
            if not field["raw_values"] or any(
                not isinstance(raw, dict) or raw.get("value") not in excerpts
                for raw in field["raw_values"]
            ):
                raise ValueError("Extracted raw values do not match fixed source evidence")
            if (
                field["status"] == "known"
                and not signature_ok
                and not _extracted_value_supported(
                    candidate, sources, bindings, executions, profile, records, content_root
                )
            ):
                raise ValueError("Extracted value does not match fixed source evidence")
    if any(
        source["kind"] == "signature" and sid not in signature_sources
        for sid, source in sources.items()
    ):
        raise ValueError("Signature source lacks a validated company candidate")
    expected_ids = {item["requirement_id"] for item in profile["requirements"]}
    assessments = handoff["requirement_assessments"]
    if {item["requirement_id"] for item in assessments} != expected_ids or len(assessments) != len(
        expected_ids
    ):
        raise ValueError("Independent requirements were omitted or duplicated")
    if any(not set(item["candidate_ids"]).issubset(candidate_ids) for item in assessments):
        raise ValueError("Requirement references an unknown candidate")
    candidates = {item["candidate_id"]: item for item in handoff["candidates"]}
    requirements = {item["requirement_id"]: item for item in profile["requirements"]}
    inventory_by_id = {item["material_id"]: item for item in read["inventory"]}
    scope_blocks = {
        eid: _blocks(execution)[1] for eid, execution in executions.items() if execution.get("ref")
    }
    issue_ids = {item["issue_id"] for item in handoff["issues"]}
    if len(issue_ids) != len(handoff["issues"]):
        raise ValueError("Duplicate issue identity")
    for issue in handoff["issues"]:
        if (
            not issue["paths"]
            or not set(issue["source_ids"]).issubset(source_ids)
            or not set(issue["material_ids"]).issubset(material_ids)
        ):
            raise ValueError("Issue references are invalid")
        for path in issue["paths"]:
            _pointer(handoff, path)
    for assessment in assessments:
        requirement = requirements[assessment["requirement_id"]]
        if not set(assessment["issue_ids"]).issubset(issue_ids):
            raise ValueError("Requirement references an unknown issue")
        material_id = requirement["material_id"]
        execution_id = inventory_by_id[material_id]["execution_id"]
        if any(
            _scope_analysis(requirement, block["text"])[1]
            for block in scope_blocks.get(execution_id, [])
        ):
            candidate_paths = {
                f"/candidates/{candidate_indexes[cid]}/field" for cid in assessment["candidate_ids"]
            }
            if assessment["state"] != "unresolved" or not any(
                issue["issue_id"] in assessment["issue_ids"]
                and issue["code"] == "object_scope_ambiguous"
                and material_id in issue["material_ids"]
                and candidate_paths.issubset(issue["paths"])
                for issue in handoff["issues"]
            ):
                raise ValueError("Ambiguous object scope is missing its unresolved assessment")
        for cid in assessment["candidate_ids"]:
            if (
                candidates[cid]["field_name"] != requirement["field_name"]
                or candidates[cid]["object_id"] != requirement["object_selector"]
            ):
                raise ValueError("Requirement candidate does not match its independent selector")
            for sid in candidates[cid]["field"]["source_ids"]:
                if sources[sid]["kind"] != requirement[
                    "source_kind"
                ] and not signature_candidates.get(cid):
                    raise ValueError("Source section kind differs from the independent profile")
                if requirement.get("object_anchor") or requirement.get("source_pattern"):
                    source = sources[sid]
                    locator = source["locator"]
                    artifact = artifacts[bindings[sid]["artifact_id"]]
                    document = json.loads(Path(artifact["ref"]).read_bytes())
                    text = _pointer(document, locator["json_pointer"])
                    if not any(
                        start <= locator["char_start"] < locator["char_end"] <= end
                        for start, end in _scoped_spans(requirement, text)
                    ):
                        raise ValueError(
                            "Candidate evidence is outside the independent object scope"
                        )
                if (
                    requirement["source_kind"] == "quoted_body"
                    and [
                        marker["excerpt"] for marker in bindings[sid]["section_evidence"]["markers"]
                    ]
                    != requirement["quoted_section_markers"]
                ):
                    raise ValueError("Quoted source markers differ from the independent profile")
        if assessment["state"] == "checked_present" and (
            not assessment["candidate_ids"]
            or any(
                candidates[cid]["field"]["status"] != "known" for cid in assessment["candidate_ids"]
            )
        ):
            raise ValueError("Present requirement lacks known evidence")
        states = [candidates[cid]["field"]["status"] for cid in assessment["candidate_ids"]]
        if assessment["state"] not in {
            "checked_present",
            "checked_absent",
            "not_applicable",
            "unresolved",
        } or (not states and not assessment["issue_ids"]):
            raise ValueError("Invalid or empty requirement assessment")
        if assessment["state"] == "checked_absent":
            if not states or any(state != "missing" for state in states):
                raise ValueError("Absent requirement does not preserve missing status")
            for dependency in requirement["scope_dependencies"]:
                if not set(dependency["refs"]).issubset(
                    handoff["coverage"][dependency["stage"]]["processed_refs"]
                ):
                    raise ValueError("Unread scope cannot establish field absence")
        if assessment["state"] == "not_applicable" and (
            not requirement.get("allow_not_applicable")
            or not assessment.get("applicability_evidence")
            or assessment["applicability_evidence"] != requirement.get("applicability_basis")
            or not states
            or any(state != "not_applicable" for state in states)
        ):
            raise ValueError("Not-applicable assessment lacks independent evidence")
    expected = {material_id + "/representation" for material_id in material_ids}
    expected_coverage, _ = _coverage(
        read["task_id"], read["inventory"], copy.deepcopy(read["executions"]), profile, task_scope
    )
    if set(handoff["coverage"]) != set(expected_coverage):
        raise ValueError("Required coverage stage is missing")
    for name, stage in handoff["coverage"].items():
        processed, unread = set(stage["processed_refs"]), set(stage["unread_refs"])
        if (
            set(stage["expected_refs"]) != expected
            or processed & unread
            or processed | unread != expected
        ):
            raise ValueError("Coverage does not account for the independent material inventory")
        if any(
            stage[key] != expected_coverage[name][key]
            for key in (
                "scope_ref",
                "scope_owner",
                "expected_refs",
                "processed_refs",
                "unread_refs",
                "exempt_refs",
                "unknown_scope",
                "requested",
                "stage_state",
                "state",
            )
        ):
            raise ValueError("Coverage differs from actual READ and independent material scope")
    if handoff["handoff_state"] not in {"partial", "complete"}:
        raise ValueError("Unsupported material handoff state")
    if handoff["handoff_state"] == "complete" and (
        handoff["execution_state"] != "finished"
        or handoff["termination_reason"] != "normal"
        or not _complete(handoff["coverage"], assessments, profile)
    ):
        raise ValueError("Complete handoff has unresolved requirements or coverage")


def validate_material_handoff(
    snapshot, *, task_scope, manifest_ref, field_profile, read_ref, content_root, field_contract_ref
):
    """Validate one handoff against the caller's manifest, profile and actual READ."""
    if field_profile is None:
        raise ValueError("required_field_profile_missing")
    manifest_bytes = Path(manifest_ref["ref"]).read_bytes()
    if _sha(manifest_bytes) != manifest_ref["sha256"]:
        raise ValueError("Independent manifest SHA-256 mismatch")
    manifest = json.loads(manifest_bytes)
    profile = _profile(field_profile, manifest)
    scope = _task_scope(task_scope, manifest, profile) if task_scope is not None else None
    read = read_result(read_ref)
    if (
        snapshot["read_result_ref"] != read_ref
        or snapshot["field_contract_source"] != field_contract_ref
        or read["manifest_sha256"] != manifest_ref["sha256"]
        or Path(read["manifest_path"]).resolve() != Path(manifest_ref["ref"]).resolve()
    ):
        raise ValueError("Independent READ, contract or manifest reference differs")
    if snapshot.get("task_scope_source") is not None and snapshot["task_scope_source"] != scope:
        raise ValueError("Result task scope differs from independent task scope")

    handoff = snapshot["material_handoff"]
    records = {item["material_id"]: item for item in manifest["materials"]}
    inventory = {item["material_id"]: item for item in read["inventory"]}
    if len(inventory) != len(read["inventory"]) or set(inventory) != set(records):
        raise ValueError("READ inventory differs from independent manifest")
    executions = {
        item["execution_id"]: item for item in read["executions"] if item.get("execution_id")
    }
    if len(executions) != len([item for item in read["executions"] if item.get("execution_id")]):
        raise ValueError("Duplicate READ execution identity")
    for mid, record in records.items():
        item = inventory[mid]
        representation = record.get("content")
        original = record.get("original")
        for descriptor in (representation, original):
            if descriptor:
                _read_content(Path(content_root).resolve(strict=True), descriptor)
        if (
            item["email_ref_id"] != record["email_ref_id"]
            or item["role_raw"] != record["role"]
            or item["kind"] != record["kind"]
            or item["representation"] != record["representation"]
            or item["representation_sha256"]
            != (representation["sha256"] if representation else None)
            or item["original_sha256"] != (original["sha256"] if original else None)
            or item["preprocessing_issues"] != record["issues"]
            or item["input_plan_id"] != record["parse_task_id"]
        ):
            raise ValueError("READ inventory occurrence differs from manifest")
        execution = executions.get(item["execution_id"])
        if item["read_state"] in {"success", "partial_success"}:
            if (
                not execution
                or not execution["reader_started"]
                or execution["execution_state"] != item["read_state"]
                or execution["input_plan_id"] != item["input_plan_id"]
                or execution["input_ref"]["sha256"] != item["representation_sha256"]
            ):
                raise ValueError("READ execution does not prove the material was read")
        elif execution is not None and execution["execution_state"] != item["read_state"]:
            raise ValueError("READ inventory and execution state differ")

    artifact_executions = {item["execution_id"] for item in handoff["artifact_refs"]}
    expected_artifacts = {eid for eid, execution in executions.items() if execution.get("ref")}
    if artifact_executions != expected_artifacts:
        raise ValueError("Handoff does not preserve all actual READ artifacts")
    for artifact in handoff["artifact_refs"]:
        expected = {
            mid
            for mid, item in inventory.items()
            if item["execution_id"] == artifact["execution_id"]
        }
        if set(artifact["material_ids"]) != expected:
            raise ValueError("READ artifact occurrence inventory differs")

    _assess(handoff, profile, read, manifest, content_root, field_contract_ref, scope)
    expected_materials = []
    for item in read["inventory"]:
        mid = item["material_id"]
        execution = executions.get(item["execution_id"])
        expected_materials.append(
            {
                "material_id": mid,
                "read_state": item["read_state"],
                "reason": item["reason"]
                or ("reader_conversion_failure" if item["read_state"] == "failure" else None),
                "preprocessing_issues": item["preprocessing_issues"],
                "read_issues": execution["issues"] if execution else [],
                "candidate_ids": [
                    c["candidate_id"]
                    for c in handoff["candidates"]
                    if c["object_id"].startswith(mid + "/")
                ],
                "issue_ids": [i["issue_id"] for i in handoff["issues"] if mid in i["material_ids"]],
            }
        )
    if snapshot["per_material_results"] != expected_materials:
        raise ValueError("Per-material unread reasons or references differ from READ")
    return {
        "result_id": handoff["result_id"],
        "revision": handoff["result_revision"],
        "handoff_state": handoff["handoff_state"],
        "task_scope_ref": scope["scope_ref"] if scope else None,
        "manifest_sha256": manifest_ref["sha256"],
        "read_sha256": read_ref["sha256"],
        "validated": True,
    }


def read_validated_result(reference, *, content_root):
    """Read a saved snapshot and recheck its evidence against the fixed READ artifacts."""
    snapshot = read_result(reference)
    read_ref = snapshot["read_result_ref"]
    read = read_result(read_ref)
    validate_material_handoff(
        snapshot,
        task_scope=snapshot.get("task_scope_source"),
        manifest_ref={"ref": read["manifest_path"], "sha256": read["manifest_sha256"]},
        field_profile=snapshot["material_handoff"]["required_field_profile"],
        read_ref=read_ref,
        content_root=content_root,
        field_contract_ref=snapshot["field_contract_source"],
    )
    return snapshot


def _agent_body_candidate(
    requirement,
    material,
    execution,
    blocks,
    records,
    content_root,
    read_ref,
    extraction_agent,
    sidecar,
    timeout,
    *,
    read_fallback=False,
):
    """Keep a proposed candidate isolated until the ordinary evidence check accepts it."""
    mid = requirement["material_id"]
    name = requirement["field_name"]
    event = {
        "material_id": mid,
        "requirement_id": requirement["requirement_id"],
        "read_sha256": read_ref["sha256"],
        "adapter": getattr(extraction_agent, "__name__", type(extraction_agent).__name__),
    }
    if sidecar is None and not read_fallback:
        return None, {**event, "outcome": "skipped", "reason": "markdown_provider_missing"}
    if sidecar is not None:
        event["markdown"] = {
            key: sidecar.get(key)
            for key in ("state", "markdown_ref", "markdown_sha256", "index_ref", "index_sha256")
        }
    if sidecar is not None and sidecar.get("state") == "failure" and not read_fallback:
        return None, {
            **event,
            "outcome": "skipped",
            "reason": sidecar.get("reason", "markdown_export_failed"),
        }
    if name == "destination":
        original = _read_content(content_root, records[mid]["content"]).decode("utf-8-sig")
        routes = current_route_destinations(blocks, original)
        if not routes:
            return None, {
                **event,
                "outcome": "skipped",
                "reason": "no_unique_current_destination_route",
            }
        block, _ = routes[0]
        matching = (
            [
                (fragment, text)
                for fragment, text in mapped_markdown_fragments(
                    sidecar, execution, mid, source_role="current", strict=True
                )
                if fragment["read_pointer"] == block["pointer"]
                and text == block["text"]
                and fragment["read_start"] == 0
            ]
            if sidecar
            else []
        )
        if len(matching) == 1:
            fragment, text = matching[0]
            eid = fragment["evidence_id"]
            evidence, mapped = (
                [{"evidence_id": eid, "markdown": text, "representation": "markdown"}],
                {eid: fragment},
            )
        else:
            evidence = [{"evidence_id": "R1", "read": block["text"], "representation": "read"}]
            mapped = {
                "R1": {
                    "read_pointer": block["pointer"],
                    "read_start": 0,
                    "read_end": len(block["text"]),
                }
            }
            event["read_fallback_reason"] = (
                "destination_route_not_mapped" if sidecar else "markdown_provider_missing"
            )
        event["representation"] = evidence[0]["representation"]
    else:
        evidence, mapped = (
            markdown_evidence(sidecar, execution, mid, strict=read_fallback)
            if sidecar
            else ([], {})
        )
    if read_fallback and name == "package_count":
        event["representation"] = "markdown" if evidence else "read"
        if evidence:
            for item in evidence:
                item["representation"] = "markdown"
        else:
            event["read_fallback_reason"] = (
                sidecar.get("reason", "no_current_mapped_package_evidence")
                if sidecar
                else "markdown_provider_missing"
            )
            for block, start, text in _scoped_blocks(requirement, blocks):
                if re.search(r"\b(?:pkgs|packages)\b", text, re.I):
                    eid = f"R{len(evidence) + 1}"
                    evidence.append({"evidence_id": eid, "read": text, "representation": "read"})
                    mapped[eid] = {
                        "read_pointer": block["pointer"],
                        "read_start": start,
                        "read_end": start + len(text),
                    }
    if not evidence:
        return None, {**event, "outcome": "skipped", "reason": "no_current_mapped_package_evidence"}
    request = {
        "material_id": mid,
        "read_sha256": read_ref["sha256"],
        "requirement_id": requirement["requirement_id"],
        "field_name": name,
        "object_id": requirement["object_selector"],
        "evidence": evidence,
    }
    if read_fallback:
        request["instruction"] = (
            FULL_CHAIN_INSTRUCTION
            if name == "package_count"
            else "Extract only the destination after 'to' in this current-body route. Cite one evidence ID."
        )
    event["request_sha256"] = _sha(_json_bytes(request))
    try:
        response = extraction_agent(request, timeout_seconds=timeout)
    except TimeoutError:
        return None, {**event, "outcome": "fallback", "reason": "timeout"}
    except AgentServiceError:
        return None, {**event, "outcome": "fallback", "reason": "service_error"}
    try:
        proposal = parse_response(
            response, requirement["requirement_id"], set(mapped), text_value=name == "destination"
        )
    except AgentResponseError as error:
        return None, {**event, "outcome": "fallback", "reason": str(error)}
    if proposal is None:
        return None, {**event, "outcome": "fallback", "reason": "abstained"}
    fragment = mapped[proposal["evidence_ids"][0]]
    block = next((item for item in blocks if item["pointer"] == fragment["read_pointer"]), None)
    if block is None:
        return None, {**event, "outcome": "fallback", "reason": "read_mapping_unavailable"}
    new_sources, new_bindings = [], []
    sid = _source(
        new_sources,
        new_bindings,
        material,
        execution,
        block,
        fragment["read_start"],
        fragment["read_end"],
        content_root,
        records[mid],
        source_kind="current_body",
    )
    field = {
        "value": proposal["value"],
        "confidence": None,
        "status": "known",
        "origin": "extracted",
        "source_ids": [sid],
        "raw_values": [{"value": new_sources[0]["excerpt"]}],
    }
    candidate = {
        "field_name": name,
        "field_type": _field_type(name),
        "object_id": requirement["object_selector"],
        "quantity_scope": _FIELD_SEMANTICS[name][0],
        "quantity_kind": _FIELD_SEMANTICS[name][1],
        "packaging_level": "unknown",
        "field": field,
    }
    if not _body_agent_field_supported(
        candidate,
        proposal["value"],
        {sid: new_sources[0]},
        {sid: new_bindings[0]},
        {execution["execution_id"]: execution},
        {"requirements": [requirement]},
        records,
        content_root,
    ):
        return None, {**event, "outcome": "fallback", "reason": "evidence_not_supported"}
    return (candidate, new_sources[0], new_bindings[0]), {
        **event,
        "outcome": "accepted",
        "reason": None,
    }


def _agent_pdf_total_candidate(
    requirement,
    material,
    execution,
    blocks,
    records,
    content_root,
    read_ref,
    extraction_agent,
    sidecar,
    timeout,
):
    """Ask for a total only after binding its label and value to one PDF row."""
    mid = requirement["material_id"]
    event = {
        "material_id": mid,
        "requirement_id": requirement["requirement_id"],
        "read_sha256": read_ref["sha256"],
        "adapter": getattr(extraction_agent, "__name__", type(extraction_agent).__name__),
    }
    if sidecar is not None:
        event["markdown"] = {
            key: sidecar.get(key)
            for key in ("state", "markdown_ref", "markdown_sha256", "index_ref", "index_sha256")
        }
    layout = _pdf_split_total(blocks, "line_gross_weight", evidence_only=True)
    if layout is None:
        return None, {
            **event,
            "outcome": "skipped",
            "reason": "total_label_value_scope_unconfirmed",
        }
    _, unit, evidence_blocks = layout  # Value, then label, as required by the common validator.
    mapped = (
        mapped_markdown_fragments(sidecar, execution, mid, source_role="attachment", strict=True)
        if sidecar
        else []
    )
    matching = []
    for block in reversed(evidence_blocks):
        matches = [
            (fragment, text)
            for fragment, text in mapped
            if fragment["source_role"] == "attachment"
            and fragment["read_pointer"] == block["pointer"]
            and fragment["read_start"] == 0
            and fragment["read_end"] == len(block["text"])
        ]
        matching.append(matches[0] if len(matches) == 1 else None)
    markdown_ok = all(matching)
    if markdown_ok:
        label, value = (item[0] for item in matching)
        markdown = Path(sidecar["markdown_ref"]).read_text(encoding="utf-8")
        markdown_ok = (
            label["markdown_end"] <= value["markdown_start"]
            and not markdown[label["markdown_end"] : value["markdown_start"]].strip()
        )
    if markdown_ok:
        evidence = [
            {
                "evidence_id": item[0]["evidence_id"],
                "markdown": item[1],
                "representation": "markdown",
            }
            for item in matching
        ]
        event["representation"] = "markdown"
    else:
        evidence = [
            {"evidence_id": f"R{i}", "read": block["text"], "representation": "read"}
            for i, block in enumerate(reversed(evidence_blocks), 1)
        ]
        event["representation"] = "read"
        event["read_fallback_reason"] = (
            sidecar.get("reason", "total_relation_not_mapped")
            if sidecar
            else "markdown_provider_missing"
        )
    request = {
        "material_id": mid,
        "read_sha256": read_ref["sha256"],
        "requirement_id": requirement["requirement_id"],
        "field_name": "line_gross_weight",
        "object_id": requirement["object_selector"],
        "instruction": FULL_CHAIN_INSTRUCTION,
        "evidence": evidence,
    }
    event["request_sha256"] = _sha(_json_bytes(request))
    try:
        response = extraction_agent(request, timeout_seconds=timeout)
    except TimeoutError:
        return None, {**event, "outcome": "fallback", "reason": "timeout"}
    except AgentServiceError:
        return None, {**event, "outcome": "fallback", "reason": "service_error"}
    try:
        proposal = parse_response(
            response,
            requirement["requirement_id"],
            {item["evidence_id"] for item in evidence},
            evidence_count=2,
        )
    except AgentResponseError as error:
        return None, {**event, "outcome": "fallback", "reason": str(error)}
    if proposal is None:
        return None, {**event, "outcome": "fallback", "reason": "abstained"}
    new_sources, new_bindings = [], []
    ids = [
        _source(
            new_sources,
            new_bindings,
            material,
            execution,
            block,
            0,
            len(block["text"]),
            content_root,
            records[mid],
            source_kind="attachment",
        )
        for block in evidence_blocks
    ]
    candidate = {
        "field_name": "line_gross_weight",
        "field_type": _field_type("line_gross_weight"),
        "object_id": requirement["object_selector"],
        "quantity_scope": _FIELD_SEMANTICS["line_gross_weight"][0],
        "quantity_kind": _FIELD_SEMANTICS["line_gross_weight"][1],
        "packaging_level": "unknown",
        "field": {
            "value": proposal["value"],
            "unit": unit,
            "confidence": None,
            "status": "known",
            "origin": "extracted",
            "source_ids": ids,
            "raw_values": [{"value": evidence_blocks[0]["text"]}],
        },
    }
    if not _pdf_split_total_supported(
        candidate,
        proposal["value"],
        {item["source_id"]: item for item in new_sources},
        {item["source_id"]: item for item in new_bindings},
        {execution["execution_id"]: execution},
        {"requirements": [requirement]},
    ):
        return None, {**event, "outcome": "fallback", "reason": "evidence_not_supported"}
    return (candidate, new_sources, new_bindings), {**event, "outcome": "accepted", "reason": None}


def _agent_unit(name):
    return {"kg_field": "kg", "cbm_field": "CBM", "cm_field": "cm"}.get(_field_type(name))


def _material_agent_proposals(
    profile,
    selected,
    inventory,
    records,
    executions,
    blocks,
    markdown_outputs,
    content_root,
    read_ref,
    agent,
    timeout,
    request_projector=None,
):
    proposals, evidence_by_material, events = {}, {}, []
    for mid in sorted(selected):
        material, record = inventory[mid], records[mid]
        execution = executions.get(material["execution_id"])
        if (
            record["kind"] == "body"
            and material["source_role"] == "current"
            and record["role"] == "current"
        ):
            role, kind = "current", "current_body"
        elif record["kind"] == "attachment" or _is_named_inline_pdf(record):
            role, kind = "attachment", "attachment"
        else:
            continue
        requirements = [
            r
            for r in profile["requirements"]
            if r["material_id"] == mid and r["source_kind"] == kind
        ]
        event = {"material_id": mid, "read_sha256": read_ref["sha256"], "stage": "material_agent"}
        if not requirements:
            continue
        if (
            not execution
            or not execution.get("ref")
            or material["read_state"] not in {"success", "partial_success"}
        ):
            events.append({**event, "outcome": "skipped", "reason": "read_unavailable"})
            continue
        original = (
            _read_content(content_root, record["content"]).decode("utf-8-sig")
            if kind == "current_body"
            else None
        )
        material_blocks = blocks.get(material["execution_id"], [])
        if kind == "current_body":
            material_blocks = [
                {**block, "text": block["text"][:end]}
                for block, end in _current_body_blocks(material_blocks)
            ]
        if profile["id"] == ATTACHMENT_WEIGHT_PROFILE:
            needed = {
                block["pointer"]
                for r in requirements
                if (
                    proof := table_weight(
                        material_blocks, execution, r["weight_table"], r["field_name"]
                    )
                )
                for block in proof[2]
            }
            material_blocks = [block for block in material_blocks if block["pointer"] in needed]
        evidence_provider = _MATERIAL_EVIDENCE_SCOPE.get() or material_evidence
        evidence, evidence_by_material[mid] = evidence_provider(
            markdown_outputs.get(mid),
            execution,
            mid,
            material_blocks,
            role,
            original,
        )
        if not evidence:
            events.append({**event, "outcome": "skipped", "reason": "no_bound_evidence"})
            continue
        requested = [
            {
                "requirement_id": r["requirement_id"],
                "field_name": r["field_name"],
                "field_type": _field_type(r["field_name"]),
                "unit": _agent_unit(r["field_name"]),
                "object": r["object_selector"].split("/", 1)[1],
                **{
                    k: r[k]
                    for k in (
                        "object_anchor",
                        "pdf_row_anchor",
                        "pdf_cargo",
                        "source_pattern",
                        "weight_table",
                    )
                    if k in r
                },
            }
            for r in requirements
        ]
        request = {
            "material_id": mid,
            "read_sha256": read_ref["sha256"],
            "source_kind": kind,
            "requirements": requested,
            "evidence": evidence,
            "source_index": {
                eid: {"pointer": item[0]["pointer"], "start": item[1], "end": item[2]}
                for eid, item in evidence_by_material[mid].items()
            },
        }
        if request_projector is not None:
            request = request_projector(request)
            requested, evidence = request["requirements"], request["evidence"]
            if not requested or not evidence:
                events.append({**event, "outcome": "skipped", "reason": "no_evidenced_gap"})
                continue
        event.update(
            request_sha256=_sha(_json_bytes(request)),
            requested_fields=len(requested),
            markdown_fragments=sum(x["representation"] == "markdown" for x in evidence),
            read_fragments=sum(x["representation"] == "read" for x in evidence),
        )
        try:
            response = agent(request, timeout_seconds=timeout)
            batch, rejected, unbound = material_candidates(
                response,
                {x["requirement_id"] for x in requested},
                {x["evidence_id"] for x in evidence},
            )
        except TimeoutError:
            events.append({**event, "outcome": "fallback", "reason": "timeout"})
        except AgentServiceError:
            events.append({**event, "outcome": "fallback", "reason": "service_error"})
        except AgentResponseError as error:
            events.append({**event, "outcome": "fallback", "reason": str(error)})
        else:
            proposals.update(batch)
            events.append(
                {
                    **event,
                    "outcome": "received",
                    "proposed": sum(map(len, batch.values())),
                    "rejected_shape": rejected,
                    "rejected_evidence": len(unbound),
                }
            )
            events.extend(
                {
                    **event,
                    "stage": "material_candidate",
                    "requirement_id": rid,
                    "outcome": "rejected",
                    "reason": "evidence_binding_failed",
                }
                for rid in unbound
            )
    return proposals, evidence_by_material, events


def _material_evidence_rejection(name, block, start, end, source_kind):
    if source_kind != "current_body":
        return None
    text = block["text"]
    if name in _BODY_ROLLUP_FIELDS and _narrow_body_scope(text, start, end):
        return "field_scope_mismatch"
    left, right = text.rfind("\n", 0, start) + 1, text.find("\n", end)
    line = text[left : right if right >= 0 else len(text)]
    if name in {"destination", "trade_terms", "package_count", "ctns", "package_type"} and (
        _AGENT_CONDITION.search(line)
        or (name == "trade_terms" and _AGENT_TERM_ALTERNATIVES.search(line))
        or (name == "destination" and _AGENT_ROUTE_ALTERNATIVES.search(line))
    ):
        return "conditional_or_alternative"
    if name in {"ctns", "package_count", "package_type"} and re.search(
        rf"\b(?:per|each)[ \t]+{_PACKAGE_SCOPE_EN}|每[ \t]*{_PACKAGE_SCOPE_ZH}",
        line,
        re.I,
    ):
        return "field_scope_mismatch"
    if (
        name == "ctns"
        and _AGENT_COUNT_UNIT.search(line)
        and not re.search(r"\b(?:cartons?|ctns?)\b", line, re.I)
    ):
        return "field_scope_mismatch"
    if (
        name == "package_count"
        and (unit := _AGENT_COUNT_UNIT.search(line))
        and unit["kind"].lower()
        in {
            "pcs",
            "piece",
            "pieces",
        }
    ):
        return "field_scope_mismatch"
    return None


def _complete_agent_read_evidence(
    cited, value, requirement, material, execution, record, content_root
):
    """Supply independently verified READ context when the Agent cites the value block."""
    field_name = requirement["field_name"]
    if (
        requirement.get("cargo_table_row")
        and requirement["source_kind"] == "attachment"
        and execution
        and len(cited) >= 1
    ):
        rows = packing_rows(_blocks(execution)[1], execution)
        proof = _packing_row_proof(rows, requirement)
        if not proof or value != proof[0]:
            return cited
        expected = {b["pointer"]: b for b in proof[2]}
        by_pointer = {}
        for b, start, end, representation in cited:
            ptr = b["pointer"]
            canonical = expected.get(ptr)
            if (
                canonical is None
                or ptr in by_pointer
                or b["text"] != canonical["text"]
                or b["text"][:start].strip()
                or b["text"][end:].strip()
                or not b["text"][start:end].strip()
            ):
                return cited
            by_pointer[ptr] = (
                (b, start, end, representation)
                if (start, end) == (0, len(b["text"]))
                else (
                    canonical,
                    0,
                    len(canonical["text"]),
                    "read",
                )
            )
        # Citation must include the actual value cell. Complete only its known
        # row/header proof. Omitting whitespace is harmless; omitting a qualifier isn't.
        if proof[2][0]["pointer"] in by_pointer:
            return [by_pointer.get(b["pointer"], (b, 0, len(b["text"]), "read")) for b in proof[2]]
        return cited
    if (
        field_name
        not in {
            "line_volume",
            "contact_phone",
            "signature_email",
            "cargo_ready_time",
            "business_remarks",
            "package_count",
            "package_type",
            "trade_terms",
            "destination",
            "cargo_type",
            "shipment_gross_weight",
        }
        or requirement["source_kind"] != "current_body"
        or requirement["object_selector"] != material["material_id"] + "/document"
        or record["kind"] != "body"
        or record["role"] != "current"
        or any(requirement.get(k) for k in ("object_anchor", "source_pattern"))
        or len(cited) not in (1, 2)
    ):
        return cited
    # Keep the existing validator for complete citations and other proof shapes.
    by_locator = {(entry[0]["pointer"], entry[1], entry[2]): entry for entry in cited}
    if len(by_locator) != len(cited):
        return cited
    scoped = list(_current_body_blocks(_blocks(execution)[1]))
    raw = _read_content(Path(content_root), record["content"]).decode("utf-8-sig")
    current = current_body_text_check(raw, allow_quoted_duplicate=True)
    if field_name in SHIPMENT_SUMMARY_FIELDS:
        for name, observed, _, proof in shipment_summary_evidence(scoped, current):
            keys = [(b["pointer"], a, z) for b, a, z in proof]
            if (
                name == field_name
                and observed == value
                and keys[0] in by_locator
                and by_locator.keys() <= set(keys)
            ):
                return [
                    by_locator.get(key, (*p, "read")) for key, p in zip(keys, proof, strict=True)
                ]
        if field_name == "shipment_gross_weight":
            return cited
    if field_name == "line_volume":
        proofs = [
            (observed, proof, -1)
            for _, observed, _, proof in split_cargo_gap_evidence(
                [b for b, end in scoped if end == len(b["text"])], current
            )
        ]
    elif field_name in {
        "package_count",
        "package_type",
        "trade_terms",
        "destination",
        "cargo_type",
    }:
        proofs = [
            (observed, proof, -1)
            for name, observed, _, proof in group_declaration_evidence(
                [{**b, "text": b["text"][:end]} for b, end in scoped],
                current,
            )
            if name == field_name
        ]
    else:
        # Existing, independently proven signatures keep their normal contract.
        if field_name in inquiry_body_details(scoped, raw):
            return cited
        detail = agent_body_detail_proofs(scoped, raw).get(field_name)
        proofs = [(*detail, 0)] if detail else []
    for observed, proof, anchor in proofs:
        if observed != value:
            continue
        keys = [(b["pointer"], start, end) for b, start, end in proof]
        if keys[anchor] in by_locator and by_locator.keys() <= set(keys):
            return [by_locator.get(key, (*p, "read")) for key, p in zip(keys, proof, strict=True)]
    return cited


def _normalize_material_value(name, value, field_type):
    """Accept equivalent output forms before checking the underlying READ proof."""
    if field_type in {"text_field", "package_field"}:
        if not isinstance(value, str) or not value.strip() or len(value) > 500:
            return None
        value = value.strip()
        if field_type == "package_field":
            canonical = re.sub(r"[ -]+", "_", value.casefold())
            return canonical if canonical in {"carton", "pallet", "wooden_case", "other"} else None
        if name == "trade_terms" and re.fullmatch(r"[A-Za-z]{3}", value):
            value = value.upper()
        if name == "cargo_type" and re.fullmatch(r"general[ \t]+cargo", value, re.I):
            value = "general cargo"
        if name in {"origin", "destination", "departure_port"} and (
            place := re.fullmatch(r"([A-Z]{3})\s+(?i:airport|port)", value)
        ):
            value = place.group(1)
        return value
    if isinstance(value, str):
        text = value.strip()
        if not re.fullmatch(r"[0-9]{1,16}(?:\.[0-9]{1,15})?", text):
            return None
        numeric = Decimal(text)
        if numeric > 10**15 or (
            field_type == "integer_field" and numeric != numeric.to_integral_value()
        ):
            return None
        value = int(numeric) if numeric == numeric.to_integral_value() else float(numeric)
        if Decimal(str(value)) != numeric:
            return None
    if type(value) not in {int, float} or not 0 <= value <= 10**15 or not math.isfinite(value):
        return None
    if field_type == "integer_field" and type(value) is float:
        return int(value) if value.is_integer() else None
    return value


def _checked_material_candidate(
    proposal, evidence, requirement, material, execution, record, records, profile, content_root
):
    name = requirement["field_name"]
    field_type = _field_type(name)
    value = _normalize_material_value(name, proposal["value"], field_type)
    if value is None:
        return None, "invalid_candidate_value"
    new_sources, new_bindings = [], []
    representations = set()
    if any(eid not in evidence for eid in proposal["evidence_ids"]):
        return None, "evidence_binding_failed"
    cited = [evidence[eid] for eid in proposal["evidence_ids"]]
    resolved = _complete_agent_read_evidence(
        cited, value, requirement, material, execution, record, content_root
    )
    for block, start, end, representation in resolved:
        if requirement.get("object_anchor") or requirement.get("source_pattern"):
            if not any(
                left <= start < end <= right
                for left, right in _scoped_spans(requirement, block["text"])
            ):
                return None, "field_scope_mismatch"
        if reason := _material_evidence_rejection(
            name, block, start, end, requirement["source_kind"]
        ):
            return None, reason
        _source(
            new_sources,
            new_bindings,
            material,
            execution,
            block,
            start,
            end,
            content_root,
            record,
            source_kind=requirement["source_kind"],
        )
        representations.add(representation)
    ids = [x["source_id"] for x in new_sources]
    excerpts = [{"value": s["excerpt"]} for s in new_sources]
    field = {
        "value": value,
        "confidence": None,
        "status": "known",
        "origin": "extracted",
        "source_ids": ids,
    }
    if unit := _agent_unit(name):
        field["unit"] = unit
    candidate = {
        "field_name": name,
        "field_type": field_type,
        "object_id": requirement["object_selector"],
        "quantity_scope": _FIELD_SEMANTICS[name][0],
        "quantity_kind": _FIELD_SEMANTICS[name][1],
        "packaging_level": "outer_carton" if name == "ctns" else "unknown",
        "field": field,
    }
    for raw in (*([x] for x in excerpts), excerpts):
        field["raw_values"] = raw
        if _extracted_value_supported(
            candidate,
            dict(zip(ids, new_sources, strict=True)),
            dict(zip(ids, new_bindings, strict=True)),
            {execution["execution_id"]: execution},
            profile,
            records,
            content_root,
            exact_evidence=True,
        ):
            return (candidate, new_sources, new_bindings, sorted(representations)), None
    return None, "evidence_not_supported"


def _material_rejection_detail(proposal, evidence, requirement, execution, reason):
    """Describe only distinctions established by the bound READ; do not change the verdict."""
    cited = [evidence[eid] for eid in proposal.get("evidence_ids", ()) if eid in evidence]
    pointers = list(dict.fromkeys(block["pointer"] for block, *_ in cited))
    if reason == "field_scope_mismatch":
        return "cargo_scope_mismatch", pointers
    if reason != "evidence_not_supported":
        return None, pointers
    blocks = _blocks(execution)[1]
    if requirement["source_kind"] == "current_body":
        current = {b["pointer"]: end for b, end in _current_body_blocks(blocks)}
        if any(
            block["pointer"] not in current or end > current[block["pointer"]]
            for block, _, end, _ in cited
        ):
            return "historical_source", pointers
        name = requirement["field_name"]
        text = " ".join(block["text"][start:end] for block, start, end, _ in cited)
        if (
            name in {"cargo_origin", "pickup_city"}
            and re.search(r"\bEXW\b", text, re.I)
            and not re.search(_LOCATION_LABELS[name], text, re.I)
        ):
            return "location_role_unconfirmed", pointers
        if name == "cargo_ready_time" and RESPONSIBILITY.search(text):
            return "field_semantics_mismatch", pointers
    selector = requirement.get("cargo_table_row")
    if selector:
        rows = list(packing_rows(blocks, execution))
        value = _normalize_material_value(
            requirement["field_name"], proposal["value"], _field_type(requirement["field_name"])
        )
        for row in rows:
            proof = row["facts"].get(requirement["field_name"])
            if (
                row["selector"] != selector
                and proof
                and proof[0] == value
                and proof[2][0]["pointer"] in pointers
            ):
                return "wrong_cargo_row", pointers
        expected = _packing_row_proof(rows, requirement)
        if (
            expected
            and expected[2][0]["pointer"] in pointers
            and any(b["pointer"] not in pointers for b in expected[2][1:])
        ):
            return "header_or_row_identity_missing", pointers
    unit = _agent_unit(requirement["field_name"])
    if unit == "kg" and any(
        re.search(r"\b\d+(?:[.,]\d+)?\s*(?:lbs?|pounds?|oz)\b", block["text"], re.I)
        for block, *_ in cited
    ):
        return "unit_mismatch", pointers
    return "value_or_scope_not_proven", pointers


def _run_main_channel_once(
    manifest_path,
    content_root,
    *,
    expected_task_id,
    expected_request_fingerprint,
    intake_context,
    selected_material_ids,
    read_configurations,
    reader_versions,
    field_profile,
    task_scope=None,
    field_contract_ref=None,
    output_dir,
    reader,
    worker_fn=None,
    confirmed_intent=None,
    extraction_agent=None,
    agent_timeout_seconds=5.0,
    markdown_provider=None,
    agent_read_fallback=False,
    agent_body_destination=False,
    material_agent=None,
    material_request_projector=None,
):
    """Read P01, extract a bounded set of fields, validate and save a local snapshot."""
    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=False)
    progress_path = root / "progress.json"
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    profile = _profile(field_profile, manifest)
    if task_scope is not None:
        task_scope = _task_scope(task_scope, manifest, profile)
    if profile is not None:
        if (
            not isinstance(field_contract_ref, dict)
            or not field_contract_ref.get("version")
            or not field_contract_ref.get("ref")
            or not field_contract_ref.get("sha256")
            or _sha(Path(field_contract_ref["ref"]).read_bytes()) != field_contract_ref["sha256"]
        ):
            raise ValueError("A verified field contract reference is required")
    progress = {
        "task_id": expected_task_id,
        "read": {"requested": True, "stage_state": "started"},
        "extraction": {"requested": True, "stage_state": "not_started", "reason": None},
    }
    _save(progress_path, progress)
    try:
        read = reader(
            manifest_path,
            content_root,
            expected_task_id=expected_task_id,
            expected_request_fingerprint=expected_request_fingerprint,
            intake_context=intake_context,
            selected_material_ids=set(selected_material_ids),
            configurations=read_configurations,
            versions=reader_versions,
            output_dir=root / "read",
            worker_fn=worker_fn,
        )
        if not read["intake_bound"]:
            raise ValueError("Task-bound intake_context is required for this local integration")
        coverage, observed = _coverage(
            expected_task_id, read["inventory"], read["executions"], profile, task_scope
        )
        read_ref = _save(root / "read-result.json", read)
        timed_out = any(
            item.get("result", {}).get("termination_reason") == "timeout"
            for item in read["executions"]
        )
        progress["read"] = {
            "requested": True,
            "stage_state": "interrupted" if timed_out else "finished",
            "result_ref": read_ref,
        }
        if profile is None:
            progress["extraction"]["reason"] = "required_field_profile_missing"
            progress_ref = _save(progress_path, progress)
            return {
                "status": "not_started",
                "reason": "required_field_profile_missing",
                "progress_ref": progress_ref,
                "read_ref": read_ref,
                "result_ref": None,
            }

        inventory = {item["material_id"]: item for item in read["inventory"]}
        records = {item["material_id"]: item for item in manifest["materials"]}
        executions = {
            item["execution_id"]: item for item in read["executions"] if item.get("execution_id")
        }
        blocks = {
            eid: _markdown_blocks(
                execution,
                records[execution["occurrence_bindings"][0]["material_id"]],
                records,
                content_root,
            )
            for eid, execution in executions.items()
            if execution.get("ref")
        }
        markdown_outputs = {}
        if markdown_provider is not None:
            for mid in selected_material_ids:
                material = inventory[mid]
                execution = executions.get(material["execution_id"])
                if (
                    not execution
                    or not execution.get("ref")
                    or material["read_state"] not in {"success", "partial_success"}
                ):
                    markdown_outputs[mid] = {
                        "state": "failure",
                        "reason": material["reason"] or "read_unavailable",
                    }
                elif execution["executed_profile"]["reader"] == "TextReader":
                    markdown_outputs[mid] = {
                        "state": "failure",
                        "reason": "legacy_text_reader_not_docling",
                    }
                else:
                    sidecar = markdown_provider(
                        execution, records[mid], records, content_root, root / "markdown" / mid
                    )
                    if sidecar.get("state") in {"success", "partial_success"}:
                        markdown_evidence(sidecar, execution, mid)
                    markdown_outputs[mid] = sidecar
            progress["markdown"] = _save(
                root / "markdown-outputs.json", {"materials": markdown_outputs}
            )
        candidates, sources, bindings, issues, assessments = [], [], [], [], []
        agent_events = []
        proposals, agent_evidence = {}, {}
        inquiry_details = {}
        cargo_rows = {}
        if material_agent is not None:
            proposals, agent_evidence, agent_events = _material_agent_proposals(
                profile,
                selected_material_ids,
                inventory,
                records,
                executions,
                blocks,
                markdown_outputs,
                content_root,
                read_ref,
                material_agent,
                agent_timeout_seconds,
                material_request_projector,
            )
        for requirement in sorted(
            profile["requirements"], key=lambda item: item["field_name"] == "unit_gross_weight"
        ):
            material_id = requirement["material_id"]
            material = inventory[material_id]
            execution = executions.get(material["execution_id"])
            selected_blocks = blocks.get(material["execution_id"], [])
            found = []
            scoped = (
                []
                if requirement.get("pdf_row_anchor")
                or requirement.get("pdf_cargo")
                or "weight_table" in requirement
                or "cargo_table_row" in requirement
                else _scoped_blocks(requirement, selected_blocks)
            )
            for block, offset, text in scoped:
                name = requirement["field_name"]
                if profile["id"] == WEIGHT_INQUIRY_PROFILE and name in BODY_WEIGHT_FIELDS:
                    if block.get("cell"):
                        continue
                    facts = _scoped_body_weight_facts(name, text)
                else:
                    facts = _facts(name, text, source_kind=requirement.get("source_kind"))
                for match, value, unit in facts:
                    source_id = _source(
                        sources,
                        bindings,
                        material,
                        execution,
                        block,
                        offset + match.start(),
                        offset + match.end(),
                        content_root,
                        records[material_id],
                        source_kind=requirement["source_kind"],
                        section=_section(requirement, selected_blocks, block),
                    )
                    candidate_id = f"C{len(candidates) + 1}"
                    raw_value = {"value": match.group(0)}
                    field = {
                        "value": value,
                        "confidence": None,
                        "status": "known",
                        "origin": "extracted",
                        "source_ids": [source_id],
                        "raw_values": [raw_value],
                    }
                    if unit:
                        field["unit"] = unit
                    field_name = requirement["field_name"]
                    candidate = {
                        "candidate_id": candidate_id,
                        "field_name": field_name,
                        "field_type": _field_type(field_name),
                        "object_id": requirement["object_selector"],
                        "quantity_scope": _FIELD_SEMANTICS[field_name][0],
                        "quantity_kind": _FIELD_SEMANTICS[field_name][1],
                        "packaging_level": "outer_carton" if field_name == "ctns" else "unknown",
                        "field": field,
                    }
                    candidates.append(candidate)
                    found.append(candidate_id)
            if (
                not found
                and requirement["field_name"]
                in DETAIL_FIELDS | SHIPMENT_SUMMARY_FIELDS | {"package_count"}
                and execution
                and selected_blocks
                and requirement["source_kind"] == "current_body"
                and material["source_role"] == "current"
                and requirement["object_selector"] == material_id + "/document"
                and not any(requirement.get(k) for k in ("object_anchor", "source_pattern"))
            ):
                record = records[material_id]
                if material_id not in inquiry_details:
                    raw = _read_content(content_root, record["content"]).decode("utf-8-sig")
                    current_blocks = list(_current_body_blocks(selected_blocks))
                    facts = {
                        name: (value, None, proof)
                        for name, (value, proof) in inquiry_body_details(
                            current_blocks, raw
                        ).items()
                    }
                    current = current_body_text_check(raw, allow_quoted_duplicate=True)
                    facts.update(
                        {
                            name: (value, unit, proof)
                            for name, value, unit, proof in shipment_summary_evidence(
                                current_blocks, current
                            )
                        }
                    )
                    facts.update(
                        {
                            name: (value, unit, proof)
                            for name, value, unit, proof in group_declaration_evidence(
                                [
                                    {**block, "text": block["text"][:end]}
                                    for block, end in current_blocks
                                ],
                                current,
                            )
                            if name == "package_count"
                        }
                    )
                    inquiry_details[material_id] = facts
                name = requirement["field_name"]
                if proof := inquiry_details[material_id].get(name):
                    value, unit, evidence = proof
                    source_ids = [
                        _source(
                            sources,
                            bindings,
                            material,
                            execution,
                            block,
                            start,
                            end,
                            content_root,
                            record,
                            source_kind="current_body",
                        )
                        for block, start, end in evidence
                    ]
                    field = {
                        "value": value,
                        "confidence": None,
                        "status": "known",
                        "origin": "extracted",
                        "source_ids": source_ids,
                        "raw_values": [
                            {"value": evidence[0][0]["text"][evidence[0][1] : evidence[0][2]]}
                        ],
                    }
                    if unit:
                        field["unit"] = unit
                    candidate_id = f"C{len(candidates) + 1}"
                    candidates.append(
                        {
                            "candidate_id": candidate_id,
                            "field_name": name,
                            "field_type": _field_type(name),
                            "object_id": requirement["object_selector"],
                            "quantity_scope": _FIELD_SEMANTICS[name][0],
                            "quantity_kind": _FIELD_SEMANTICS[name][1],
                            "packaging_level": "unknown",
                            "field": field,
                        }
                    )
                    found.append(candidate_id)
            if (
                requirement.get("source_kind") == "current_body"
                and execution
                and execution["executed_profile"]["reader"] in {"DoclingHtml", "DoclingMarkdown"}
            ):
                current_blocks = [
                    block
                    for block in selected_blocks
                    if block.get("markdown_section", "current") == "current"
                ]
                for value, unit, evidence in _body_total_fields(current_blocks, requirement):
                    source_ids = [
                        _source(
                            sources,
                            bindings,
                            material,
                            execution,
                            block,
                            0,
                            len(block["text"]),
                            content_root,
                            records[material_id],
                            source_kind="current_body",
                        )
                        for block in evidence
                    ]
                    field_name = requirement["field_name"]
                    field = {
                        "value": value,
                        "confidence": None,
                        "status": "known",
                        "origin": "extracted",
                        "source_ids": source_ids,
                        "raw_values": [{"value": evidence[0]["text"]}],
                    }
                    if unit:
                        field["unit"] = unit
                    candidate_id = f"C{len(candidates) + 1}"
                    candidates.append(
                        {
                            "candidate_id": candidate_id,
                            "field_name": field_name,
                            "field_type": _field_type(field_name),
                            "object_id": requirement["object_selector"],
                            "quantity_scope": _FIELD_SEMANTICS[field_name][0],
                            "quantity_kind": _FIELD_SEMANTICS[field_name][1],
                            "packaging_level": "outer_carton"
                            if field_name == "ctns"
                            else "unknown",
                            "field": field,
                        }
                    )
                    found.append(candidate_id)
            if (
                requirement.get("pdf_row_anchor")
                and not requirement.get("pdf_cargo")
                and execution
                and execution["executed_profile"]["reader"] in PDF_READERS
            ):
                layout = _pdf_row_field(
                    selected_blocks, requirement["field_name"], requirement["pdf_row_anchor"]
                )
                if layout is not None:
                    value, evidence_blocks = layout
                    source_ids = [
                        _source(
                            sources,
                            bindings,
                            material,
                            execution,
                            block,
                            0,
                            len(block["text"]),
                            content_root,
                            records[material_id],
                            source_kind="attachment",
                        )
                        for block in evidence_blocks
                    ]
                    field_name = requirement["field_name"]
                    field = {
                        "value": value,
                        "confidence": None,
                        "status": "known",
                        "origin": "extracted",
                        "source_ids": source_ids,
                        "raw_values": [{"value": evidence_blocks[0]["text"]}],
                    }
                    if field_name != "ctns":
                        field["unit"] = "CBM" if field_name == "line_volume" else "kg"
                    candidate_id = f"C{len(candidates) + 1}"
                    quantity_scope, quantity_kind = _FIELD_SEMANTICS[field_name]
                    candidates.append(
                        {
                            "candidate_id": candidate_id,
                            "field_name": field_name,
                            "field_type": "integer_field"
                            if field_name == "ctns"
                            else "cbm_field"
                            if field_name == "line_volume"
                            else "kg_field",
                            "object_id": requirement["object_selector"],
                            "quantity_scope": quantity_scope,
                            "quantity_kind": quantity_kind,
                            "packaging_level": "outer_carton"
                            if field_name == "ctns"
                            else "unknown",
                            "field": field,
                        }
                    )
                    found.append(candidate_id)
            if (
                requirement.get("pdf_cargo")
                and execution
                and execution["executed_profile"]["reader"] in PDF_READERS
            ):
                for value, unit, raw_blocks, evidence_blocks in _pdf_cargo_candidates(
                    selected_blocks, requirement
                ):
                    source_ids = [
                        _source(
                            sources,
                            bindings,
                            material,
                            execution,
                            block,
                            0,
                            len(block["text"]),
                            content_root,
                            records[material_id],
                            source_kind="attachment",
                        )
                        for block in evidence_blocks
                    ]
                    name = requirement["field_name"]
                    field = {
                        "value": value,
                        "confidence": None,
                        "status": "known",
                        "origin": "extracted",
                        "source_ids": source_ids,
                        "raw_values": [{"value": block["text"]} for block in raw_blocks],
                    }
                    if unit:
                        field["unit"] = unit
                    candidate_id = f"C{len(candidates) + 1}"
                    quantity_scope, quantity_kind = _FIELD_SEMANTICS[name]
                    candidates.append(
                        {
                            "candidate_id": candidate_id,
                            "field_name": name,
                            "field_type": _field_type(name),
                            "object_id": requirement["object_selector"],
                            "quantity_scope": quantity_scope,
                            "quantity_kind": quantity_kind,
                            "packaging_level": "outer_carton" if name == "ctns" else "unknown",
                            "field": field,
                        }
                    )
                    found.append(candidate_id)
            if (
                not found
                and requirement.get("pdf_total_volume")
                and execution
                and execution["executed_profile"]["reader"] in PDF_READERS
            ):
                layout = _pdf_total_volume(selected_blocks)
                if layout is not None:
                    value, evidence_blocks = layout
                    source_ids = [
                        _source(
                            sources,
                            bindings,
                            material,
                            execution,
                            block,
                            0,
                            len(block["text"]),
                            content_root,
                            records[material_id],
                            source_kind="attachment",
                        )
                        for block in evidence_blocks
                    ]
                    candidate_id = f"C{len(candidates) + 1}"
                    candidates.append(
                        {
                            "candidate_id": candidate_id,
                            "field_name": "line_volume",
                            "field_type": "cbm_field",
                            "object_id": requirement["object_selector"],
                            "quantity_scope": "line_total",
                            "quantity_kind": "volume",
                            "packaging_level": "unknown",
                            "field": {
                                "value": value,
                                "unit": "CBM",
                                "confidence": None,
                                "status": "known",
                                "origin": "extracted",
                                "source_ids": source_ids,
                                "raw_values": [{"value": evidence_blocks[0]["text"]}],
                            },
                        }
                    )
                    found.append(candidate_id)
            if (
                not found
                and requirement.get("pdf_split_total")
                and execution
                and execution["executed_profile"]["reader"] in PDF_READERS
            ):
                layout = _pdf_split_total(selected_blocks, requirement["field_name"])
                if layout is not None:
                    value, unit, evidence_blocks = layout
                    source_ids = [
                        _source(
                            sources,
                            bindings,
                            material,
                            execution,
                            block,
                            0,
                            len(block["text"]),
                            content_root,
                            records[material_id],
                            source_kind="attachment",
                        )
                        for block in evidence_blocks
                    ]
                    field_name = requirement["field_name"]
                    candidate_id = f"C{len(candidates) + 1}"
                    quantity_scope, quantity_kind = _FIELD_SEMANTICS[field_name]
                    candidates.append(
                        {
                            "candidate_id": candidate_id,
                            "field_name": field_name,
                            "field_type": "cbm_field"
                            if field_name == "line_volume"
                            else "kg_field",
                            "object_id": requirement["object_selector"],
                            "quantity_scope": quantity_scope,
                            "quantity_kind": quantity_kind,
                            "packaging_level": "unknown",
                            "field": {
                                "value": value,
                                "unit": unit,
                                "confidence": None,
                                "status": "known",
                                "origin": "extracted",
                                "source_ids": source_ids,
                                "raw_values": [{"value": evidence_blocks[0]["text"]}],
                            },
                        }
                    )
                    found.append(candidate_id)
            if execution and requirement["source_kind"] == "attachment":
                if "cargo_table_row" in requirement:
                    if material_id not in cargo_rows:
                        cargo_rows[material_id] = list(packing_rows(selected_blocks, execution))
                    proof = _packing_row_proof(cargo_rows[material_id], requirement)
                    totals = [proof] if proof else []
                elif "weight_table" in requirement:
                    proof = table_weight(
                        selected_blocks,
                        execution,
                        requirement["weight_table"],
                        requirement["field_name"],
                    )
                    totals = (
                        [(_decimal_value(_kg(proof[0], proof[1])), "kg", proof[2])] if proof else []
                    )
                else:
                    totals = _attachment_document_totals(
                        selected_blocks, requirement, execution["executed_profile"]["reader"]
                    )
                for value, unit, evidence_blocks in totals:
                    if any(
                        c["candidate_id"] in found and c["field"]["value"] == value
                        for c in candidates
                    ):
                        continue
                    source_ids = [
                        _source(
                            sources,
                            bindings,
                            material,
                            execution,
                            block,
                            0,
                            len(block["text"]),
                            content_root,
                            records[material_id],
                            source_kind="attachment",
                        )
                        for block in evidence_blocks
                    ]
                    candidate_id = f"C{len(candidates) + 1}"
                    name = requirement["field_name"]
                    field = {
                        "value": value,
                        "confidence": None,
                        "status": "known",
                        "origin": "extracted",
                        "source_ids": source_ids,
                        "raw_values": [{"value": evidence_blocks[0]["text"]}],
                    }
                    if unit:
                        field["unit"] = unit
                    candidates.append(
                        {
                            "candidate_id": candidate_id,
                            "field_name": name,
                            "field_type": _field_type(name),
                            "object_id": requirement["object_selector"],
                            "quantity_scope": _FIELD_SEMANTICS[name][0],
                            "quantity_kind": _FIELD_SEMANTICS[name][1],
                            "packaging_level": "outer_carton" if name == "ctns" else "unknown",
                            "field": field,
                        }
                    )
                    found.append(candidate_id)
            if (
                not found
                and not requirement.get("pdf_row_anchor")
                and not requirement.get("pdf_cargo")
                and "cargo_table_row" not in requirement
            ):
                derived = _derive_unit(
                    requirement,
                    selected_blocks,
                    candidates,
                    sources,
                    bindings,
                    material,
                    execution,
                    content_root,
                    records[material_id],
                )
                if derived:
                    candidates.append(derived)
                    found.append(derived["candidate_id"])
            rule_found = set(found)
            for proposal in proposals.get(requirement["requirement_id"], []):
                event = {
                    "material_id": material_id,
                    "requirement_id": requirement["requirement_id"],
                    "stage": "material_candidate",
                    "proposed_value": proposal["value"],
                }
                checked, reason = (
                    (None, "field_scope_mismatch")
                    if any(_scope_analysis(requirement, b["text"])[1] for b in selected_blocks)
                    else (
                        _checked_material_candidate(
                            proposal,
                            agent_evidence[material_id],
                            requirement,
                            material,
                            execution,
                            records[material_id],
                            records,
                            profile,
                            content_root,
                        )
                    )
                )
                if checked is None:
                    detail, pointers = _material_rejection_detail(
                        proposal, agent_evidence[material_id], requirement, execution, reason
                    )
                    if detail == "value_or_scope_not_proven" and any(
                        c["candidate_id"] in rule_found
                        and c["field"].get("value") is not None
                        and str(c["field"]["value"]).strip().casefold()
                        == str(proposal["value"]).strip().casefold()
                        for c in candidates
                    ):
                        detail = "same_rule_value_candidate_proof_unconfirmed"
                    event.update(field_name=requirement["field_name"], evidence_pointers=pointers)
                    if detail:
                        event["reason_detail"] = detail
                    agent_events.append({**event, "outcome": "rejected", "reason": reason})
                    continue
                candidate, new_sources, new_bindings, representations = checked
                event["representations"] = representations
                if len(new_sources) > len(proposal["evidence_ids"]):
                    event["read_context_added"] = len(new_sources) - len(proposal["evidence_ids"])
                if candidate["field"]["value"] != proposal["value"]:
                    event["normalized_value"] = candidate["field"]["value"]
                if any(
                    c["candidate_id"] in found
                    and c["field"]["value"] == candidate["field"]["value"]
                    and c["field"].get("unit") == candidate["field"].get("unit")
                    for c in candidates
                ):
                    agent_events.append({**event, "outcome": "corroborated"})
                    continue
                ids = []
                for source, binding in zip(new_sources, new_bindings, strict=True):
                    sid = f"S{len(sources) + 1}"
                    source["source_id"] = binding["source_id"] = sid
                    sources.append(source)
                    bindings.append(binding)
                    ids.append(sid)
                candidate["field"]["source_ids"] = ids
                candidate["candidate_id"] = f"C{len(candidates) + 1}"
                candidates.append(candidate)
                found.append(candidate["candidate_id"])
                agent_events.append(
                    {**event, "outcome": "accepted", "candidate_id": candidate["candidate_id"]}
                )
            issue_ids = []
            selected_candidates = [
                candidate for candidate in candidates if candidate["candidate_id"] in found
            ]
            distinct_values = {
                str(candidate["field"]["value"]) for candidate in selected_candidates
            }
            if len(distinct_values) > 1:
                issue_id = f"I{len(issues) + 1}"
                for candidate in selected_candidates:
                    candidate["field"]["value"] = None
                    candidate["field"]["status"] = "conflict"
                issues.append(
                    {
                        "issue_id": issue_id,
                        "category": "conflict",
                        "code": "CONFLICTING_VALUES",
                        "severity": "warning",
                        "paths": [
                            f"/candidates/{candidates.index(c)}/field" for c in selected_candidates
                        ],
                        "source_ids": [
                            sid for c in selected_candidates for sid in c["field"]["source_ids"]
                        ],
                        "material_ids": [material_id],
                        "message": "Distinct values require review",
                    }
                )
                issue_ids.append(issue_id)
            body_agent_fields = (
                {"package_count", "destination"}
                if agent_body_destination and agent_read_fallback
                else {"package_count"}
            )
            if (
                not found
                and extraction_agent is not None
                and requirement["field_name"] in body_agent_fields
                and requirement["source_kind"] == "current_body"
            ):
                same_material = [
                    item
                    for item in profile["requirements"]
                    if item["field_name"] == requirement["field_name"]
                    and item["material_id"] == material_id
                ]
                record = records[material_id]
                eligible = (
                    len(same_material) == 1
                    and requirement["object_selector"] == material_id + "/document"
                    and record["kind"] == "body"
                    and record["role"] == "current"
                    and material["source_role"] == "current"
                    and not any(
                        _scope_analysis(requirement, block["text"])[1] for block in selected_blocks
                    )
                    and record.get("format") in {"html", "text"}
                    and execution
                    and execution.get("execution_state") in {"success", "partial_success"}
                    and execution.get("executed_profile", {}).get("reader")
                    in {"DoclingHtml", "DoclingText"}
                )
                if eligible:
                    proposed, event = _agent_body_candidate(
                        requirement,
                        material,
                        execution,
                        selected_blocks,
                        records,
                        content_root,
                        read_ref,
                        extraction_agent,
                        markdown_outputs.get(material_id),
                        agent_timeout_seconds,
                        read_fallback=agent_read_fallback,
                    )
                    agent_events.append(event)
                    if proposed:
                        candidate, source, binding = proposed
                        sid = f"S{len(sources) + 1}"
                        source["source_id"] = binding["source_id"] = sid
                        candidate["field"]["source_ids"] = [sid]
                        candidate["candidate_id"] = f"C{len(candidates) + 1}"
                        sources.append(source)
                        bindings.append(binding)
                        candidates.append(candidate)
                        found.append(candidate["candidate_id"])
                else:
                    agent_events.append(
                        {
                            "material_id": material_id,
                            "requirement_id": requirement["requirement_id"],
                            "outcome": "skipped",
                            "reason": "material_or_object_not_eligible",
                        }
                    )
            if (
                not found
                and agent_read_fallback
                and extraction_agent is not None
                and requirement["field_name"] == "line_gross_weight"
                and requirement.get("pdf_split_total")
                and requirement["source_kind"] == "attachment"
            ):
                same_material = [
                    item
                    for item in profile["requirements"]
                    if item["field_name"] == "line_gross_weight"
                    and item["material_id"] == material_id
                ]
                record = records[material_id]
                eligible = (
                    len(same_material) == 1
                    and requirement["object_selector"] == material_id + "/document"
                    and (record["kind"] == "attachment" or _is_named_inline_pdf(record))
                    and record.get("format") == "pdf"
                    and execution
                    and execution.get("execution_state") in {"success", "partial_success"}
                    and execution.get("executed_profile", {}).get("reader") in PDF_READERS
                )
                if eligible:
                    proposed, event = _agent_pdf_total_candidate(
                        requirement,
                        material,
                        execution,
                        selected_blocks,
                        records,
                        content_root,
                        read_ref,
                        extraction_agent,
                        markdown_outputs.get(material_id),
                        agent_timeout_seconds,
                    )
                    agent_events.append(event)
                    if proposed:
                        candidate, new_sources, new_bindings = proposed
                        ids = []
                        for source, binding in zip(new_sources, new_bindings, strict=True):
                            sid = f"S{len(sources) + 1}"
                            source["source_id"] = binding["source_id"] = sid
                            sources.append(source)
                            bindings.append(binding)
                            ids.append(sid)
                        candidate["field"]["source_ids"] = ids
                        candidate["candidate_id"] = f"C{len(candidates) + 1}"
                        candidates.append(candidate)
                        found.append(candidate["candidate_id"])
                else:
                    agent_events.append(
                        {
                            "material_id": material_id,
                            "requirement_id": requirement["requirement_id"],
                            "outcome": "skipped",
                            "reason": "material_or_object_not_eligible",
                        }
                    )
            if not found:
                issue_id = f"I{len(issues) + 1}"
                reason = "field_not_found_or_scope_unread"
                if requirement.get("pdf_cargo") and selected_blocks:
                    name = requirement["field_name"]
                    if name in {"length_cm", "width_cm", "height_cm"}:
                        if requirement.get("pdf_row_anchor") and _pdf_row_pallet_measurement(
                            selected_blocks, requirement["pdf_row_anchor"]
                        ):
                            reason = "pallet_measurement_not_carton_dimensions"
                        else:
                            reason = "dimensions_order_unit_or_carton_scope_unconfirmed"
                    elif name == "product_name" and not requirement.get("pdf_row_anchor"):
                        reason = "document_product_row_not_selected"
                    elif name == "package_type":
                        package = (
                            _pdf_document_package(selected_blocks)
                            if not requirement.get("pdf_row_anchor")
                            else None
                        )
                        if package and package[2] is None:
                            reason = "package_type_unspecified"
                linked = []
                if requirement["field_name"] == "line_volume" and not requirement.get(
                    "pdf_row_anchor"
                ):
                    for block, offset, text in _scoped_blocks(requirement, selected_blocks):
                        for other_id, other in records.items():
                            filename = other.get("filename")
                            if not filename or inventory[other_id]["read_state"] in {
                                "success",
                                "partial_success",
                            }:
                                continue
                            pattern = re.compile(
                                r"(?:体积|volume)[^。\n]{0,60}?(?:见|see|refer\s+to)[^。\n]{0,40}?"
                                + re.escape(filename),
                                re.I,
                            )
                            match = pattern.search(text)
                            if match:
                                sid = _source(
                                    sources,
                                    bindings,
                                    material,
                                    execution,
                                    block,
                                    offset + match.start(),
                                    offset + match.end(),
                                    content_root,
                                    records[material_id],
                                    source_kind=requirement["source_kind"],
                                    section=_section(requirement, selected_blocks, block),
                                )
                                linked.append((other_id, sid))
                    if linked:
                        reason = "explicitly_referenced_material_unread"
                issues.append(
                    {
                        "issue_id": issue_id,
                        "category": "unread" if linked else "uncertain",
                        "code": reason,
                        "severity": "warning",
                        "paths": [f"/candidates/{len(candidates)}/field"],
                        "source_ids": [sid for _, sid in linked],
                        "material_ids": [mid for mid, _ in linked] or [material_id],
                        "message": reason,
                    }
                )
                issue_ids.append(issue_id)
                candidate_id = f"C{len(candidates) + 1}"
                name = requirement["field_name"]
                candidates.append(
                    {
                        "candidate_id": candidate_id,
                        "field_name": name,
                        "field_type": _field_type(name),
                        "object_id": requirement["object_selector"],
                        "quantity_scope": _FIELD_SEMANTICS[name][0],
                        "quantity_kind": _FIELD_SEMANTICS[name][1],
                        "packaging_level": "unknown",
                        "field": {
                            "value": None,
                            "confidence": None,
                            "status": "uncertain",
                            "origin": "unknown",
                            "source_ids": [],
                            "raw_values": [],
                        },
                    }
                )
                found.append(candidate_id)
            if any(_scope_analysis(requirement, block["text"])[1] for block in selected_blocks):
                issue_id = f"I{len(issues) + 1}"
                issues.append(
                    {
                        "issue_id": issue_id,
                        "category": "uncertain",
                        "code": "object_scope_ambiguous",
                        "severity": "warning",
                        "paths": [
                            f"/candidates/{index}/field"
                            for index, candidate in enumerate(candidates)
                            if candidate["candidate_id"] in found
                        ],
                        "source_ids": [],
                        "material_ids": [material_id],
                        "message": "Multiple object labels share a line; field scope requires review",
                    }
                )
                issue_ids.append(issue_id)
            assessments.append(
                {
                    "requirement_id": requirement["requirement_id"],
                    "state": "unresolved" if issue_ids else "checked_present",
                    "candidate_ids": found,
                    "issue_ids": issue_ids,
                }
            )

        artifacts = [
            {
                "artifact_id": x["execution_id"],
                "kind": x["structure_refs"][0]["kind"],
                "ref": x["ref"],
                "producer": x["executed_profile"]["reader"],
                "artifact_hash": x["artifact_hash"],
                "material_ids": [
                    m["material_id"]
                    for m in read["inventory"]
                    if m["execution_id"] == x["execution_id"]
                ],
                "input_representation_hash": x["input_ref"]["sha256"],
                "execution_id": x["execution_id"],
                "input_plan_id": x["input_plan_id"],
            }
            for x in executions.values()
            if x.get("ref")
        ]
        material_versions = []
        for item in read["inventory"]:
            record = records[item["material_id"]]
            descriptor = record.get("content")
            material_versions.append(
                {
                    "material_id": item["material_id"],
                    "material_revision": 1,
                    "content_hash": item["original_sha256"],
                    "availability": "available" if descriptor else "missing",
                    "source_email_ref_id": item["email_ref_id"],
                    "source_role": item["source_role"],
                    "role_raw": item["role_raw"],
                    "kind": item["kind"],
                    "representation": item["representation"],
                    "representation_revision": 1,
                    "representation_ref": {
                        "ref": str((Path(content_root) / descriptor["path"]).resolve()),
                        "sha256": descriptor["sha256"],
                    }
                    if descriptor
                    else None,
                    "raw_ref": str((Path(content_root) / record["original"]["path"]).resolve())
                    if record.get("original")
                    else None,
                    "original_status": "available" if item["original_sha256"] else "unavailable",
                    "parent_material_id": item["parent_material_id"],
                }
            )
        material_by_id = {item["material_id"]: item for item in material_versions}
        if artifacts:
            (root / "parsed-artifacts").mkdir()
        for artifact in artifacts:
            execution = executions[artifact["execution_id"]]
            executed_profile = copy.deepcopy(execution["executed_profile"])
            versions = executed_profile["versions"]
            executed_profile["version"] = versions.get("text_reader") or versions.get("docling")
            bindings_for_execution = []
            for mid in artifact["material_ids"]:
                material = material_by_id[mid]
                bindings_for_execution.append(
                    {
                        "material_id": mid,
                        "email_ref_id": material["source_email_ref_id"],
                        "role_raw": material["role_raw"],
                        "source_role": material["source_role"],
                        "material_revision": material["material_revision"],
                        "representation": material["representation"],
                        "representation_ref": material["representation_ref"],
                        "original_ref": {
                            "ref": material["raw_ref"],
                            "sha256": material["content_hash"],
                        }
                        if material["raw_ref"]
                        else None,
                    }
                )
            parsed = {
                "artifact_spec_version": "parsed-artifact/0.1-draft",
                "artifact_id": artifact["artifact_id"],
                "artifact_hash": artifact["artifact_hash"],
                "execution_id": artifact["execution_id"],
                "ref": artifact["ref"],
                "input_manifest_ref": {
                    "ref": read["manifest_path"],
                    "sha256": read["manifest_sha256"],
                },
                "input_plan_id": artifact["input_plan_id"],
                "input_ref": execution["input_ref"],
                "executed_profile": executed_profile,
                "occurrence_bindings": bindings_for_execution,
                "execution_state": "interrupted"
                if execution["result"].get("termination_reason") == "timeout"
                else "finished",
                "termination_reason": "deadline"
                if execution["result"].get("termination_reason") == "timeout"
                else "normal",
                "reader_conversion_state": execution["execution_state"],
                "coverage": execution["coverage"],
                "issues": execution["issues"],
            }
            parsed_ref = _save(
                root / "parsed-artifacts" / (artifact["execution_id"] + ".json"), parsed
            )
            artifact["parsed_artifact_ref"] = parsed_ref["ref"]
            artifact["parsed_artifact_sha256"] = parsed_ref["sha256"]
        handoff = {
            "handoff_spec_version": "0.3.0-draft",
            "task_id": expected_task_id,
            "email_ref_id": manifest["emails"][0]["email_ref_id"],
            "result_id": uuid4().hex,
            "result_revision": 1,
            "base_revision": None,
            "result_scope": "material_handoff",
            "field_contract_ref": {
                "version": field_contract_ref["version"],
                "sha256": field_contract_ref["sha256"],
            },
            "required_field_profile": profile,
            "execution_state": "interrupted" if timed_out else "finished",
            "handoff_state": "complete"
            if not timed_out and _complete(coverage, assessments, profile)
            else "partial",
            "input_manifest_ref": {"ref": read["manifest_path"], "sha256": read["manifest_sha256"]},
            "material_versions": material_versions,
            "artifact_refs": artifacts,
            "coverage": coverage,
            "sources": sources,
            "source_bindings": bindings,
            "candidates": candidates,
            "issues": issues,
            "requirement_assessments": assessments,
            "review_items": [],
            "termination_reason": "deadline" if timed_out else "normal",
            "supersedes_result": None,
        }
        per_material = []
        for item in read["inventory"]:
            mid = item["material_id"]
            execution = executions.get(item["execution_id"])
            per_material.append(
                {
                    "material_id": mid,
                    "read_state": item["read_state"],
                    "reason": item["reason"]
                    or ("reader_conversion_failure" if item["read_state"] == "failure" else None),
                    "preprocessing_issues": item["preprocessing_issues"],
                    "read_issues": execution["issues"] if execution else [],
                    "candidate_ids": [
                        c["candidate_id"]
                        for c in candidates
                        if c["object_id"].startswith(mid + "/")
                    ],
                    "issue_ids": [i["issue_id"] for i in issues if mid in i["material_ids"]],
                }
            )
        agent_called = any(event.get("request_sha256") for event in agent_events)
        if extraction_agent is not None or material_agent is not None:
            progress["agent"] = _save(root / "agent-events.json", {"events": agent_events})
        snapshot = {
            "kind": "local_material_handoff_subset",
            "contract_check": "local_reference_requirements_coverage_check_passed",
            "intent": {"confirmed_by_caller": confirmed_intent, "recognition_invoked": False},
            "analysis": {
                "method": "bounded_explicit_patterns_and_validated_adapter"
                if agent_called
                else "bounded_explicit_patterns",
                "observed_read_scope": observed,
                "limitations": [
                    "Extraction adapter invoked; intent recognition not invoked"
                    if agent_called
                    else "No language model or intent recognition invoked",
                    "Pattern extraction is not exhaustive",
                    "Cross-material business reconciliation is not performed",
                ],
            },
            "material_handoff": handoff,
            "per_material_results": per_material,
            "read_result_ref": read_ref,
            "field_contract_source": field_contract_ref,
        }
        if task_scope is not None:
            snapshot["task_scope_source"] = task_scope
        validate_material_handoff(
            snapshot,
            task_scope=task_scope,
            manifest_ref={"ref": read["manifest_path"], "sha256": read["manifest_sha256"]},
            field_profile=profile,
            read_ref=read_ref,
            content_root=content_root,
            field_contract_ref=field_contract_ref,
        )
        result_ref = _save(root / "material-result.json", snapshot)
        read_result(result_ref)
        progress["extraction"] = {"requested": True, "stage_state": "finished", "reason": None}
        progress["result_ref"] = result_ref
        _save(progress_path, progress)
        return {"status": handoff["handoff_state"], "result_ref": result_ref, "read_ref": read_ref}
    except Exception as error:
        progress["failure"] = {"type": type(error).__name__, "message": str(error)}
        _save(progress_path, progress)
        raise


def _field_states(snapshot):
    return {
        item["field_name"]: item["field"] for item in snapshot["material_handoff"]["candidates"]
    }


def run_main_channel(
    manifest_path,
    content_root,
    *,
    expected_task_id,
    expected_request_fingerprint,
    intake_context,
    selected_material_ids,
    read_configurations,
    reader_versions,
    field_profile,
    task_scope=None,
    field_contract_ref=None,
    output_dir,
    reader,
    worker_fn=None,
    confirmed_intent=None,
    pdf_support=None,
    extraction_agent=None,
    agent_timeout_seconds=5.0,
    markdown_provider=None,
    agent_read_fallback=False,
    agent_body_destination=False,
    material_agent=None,
    material_request_projector=None,
):
    """Read, extract and save a result; optional PDF support also runs bounded supplements."""
    selected_material_ids = set(selected_material_ids)
    if (
        pdf_support is not None
        and read_configurations.get(("docling", "pdf"), {}).get("reader") in PDF_STRUCTURED_READERS
    ):
        raise ValueError(
            "Structured PDF first read cannot be combined with supplementary PDF passes"
        )
    root = Path(output_dir).resolve()
    agent_cache = {}
    material_cache = {}

    def bounded_material_agent(request, *, timeout_seconds):
        mid = request["material_id"]
        signature = _sha(_json_bytes({k: v for k, v in request.items() if k != "read_sha256"}))
        if mid in material_cache:
            previous, response, failure = material_cache[mid]
            if signature != previous:
                return {"candidates": []}
            if failure:
                raise failure()
            return response
        try:
            response = material_agent(request, timeout_seconds=timeout_seconds)
        except (TimeoutError, AgentServiceError) as error:
            material_cache[mid] = (signature, None, type(error))
            raise
        material_cache[mid] = (signature, response, None)
        return response

    def bounded_agent(request, *, timeout_seconds):
        mid = (
            (request["material_id"], request["field_name"])
            if agent_body_destination
            else request["material_id"]
        )
        signature = _sha(
            _json_bytes({key: value for key, value in request.items() if key != "read_sha256"})
        )
        if mid in agent_cache:
            previous, response, failure = agent_cache[mid]
            if signature != previous:
                return {"requirement_id": request["requirement_id"], "candidate": None}
            if failure:
                raise failure()
            return response
        try:
            response = extraction_agent(request, timeout_seconds=timeout_seconds)
        except (TimeoutError, AgentServiceError) as error:
            agent_cache[mid] = (signature, None, type(error))
            raise
        agent_cache[mid] = (signature, response, None)
        return response

    if extraction_agent is not None:
        bounded_agent.__name__ = getattr(
            extraction_agent, "__name__", type(extraction_agent).__name__
        )

    def remaining():
        deadline = pdf_support.get("deadline")
        return (
            max(0.001, deadline - time.monotonic())
            if deadline
            else pdf_support.get("task_timeout", 180)
        )

    def adaptive(request, **options):
        if request.get("format") != "pdf":
            return worker_fn(request)
        return pdf_support["read_pdf_adaptive"](
            request,
            worker_fn=worker_fn,
            document_timeout=pdf_support["document_timeout"],
            supplement_timeout=pdf_support["supplement_timeout"],
            task_timeout=remaining(),
            **options,
        )

    def keeps_conflicts(previous, updated):
        return material_agent is None or all(
            updated.get(name, {}).get("status") == "conflict"
            for name, field in previous.items()
            if field["status"] == "conflict"
        )

    def once(path, configs, selected_worker):
        return _run_main_channel_once(
            manifest_path,
            content_root,
            expected_task_id=expected_task_id,
            expected_request_fingerprint=expected_request_fingerprint,
            intake_context=intake_context,
            selected_material_ids=selected_material_ids,
            read_configurations=configs,
            reader_versions=reader_versions,
            field_profile=field_profile,
            task_scope=task_scope,
            field_contract_ref=field_contract_ref,
            output_dir=path,
            reader=reader,
            worker_fn=selected_worker,
            confirmed_intent=confirmed_intent,
            extraction_agent=bounded_agent if extraction_agent is not None else None,
            agent_timeout_seconds=agent_timeout_seconds,
            markdown_provider=markdown_provider,
            agent_read_fallback=agent_read_fallback,
            agent_body_destination=agent_body_destination,
            material_agent=bounded_material_agent if material_agent is not None else None,
            material_request_projector=material_request_projector,
        )

    primary_worker = worker_fn
    if pdf_support is not None:
        primary_worker = adaptive
        ocr_page = pdf_support.get("ocr_page")
        if ocr_page is not None and len(selected_material_ids) == 1:
            primary_worker = partial(adaptive, ocr_page=ocr_page)
    primary = once(root, read_configurations, primary_worker)
    if (
        pdf_support is None
        or primary.get("result_ref") is None
        or (field_profile and field_profile["id"] == ATTACHMENT_WEIGHT_PROFILE)
    ):
        return primary
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    targets = [
        item
        for item in manifest["materials"]
        if item["material_id"] in selected_material_ids
        and item.get("format") == "pdf"
        and item["state"] == "ready"
    ]
    if len(targets) != 1:
        if len(targets) > 1:
            primary["pdf_supplement"] = {
                "state": "not_triggered",
                "pages": [],
                "reason": "multiple_selected_pdfs",
            }
        return primary
    target = targets[0]
    snapshot = read_validated_result(primary["result_ref"], content_root=content_root)
    read = read_result(primary["read_ref"])
    observed = next(
        item for item in read["inventory"] if item["material_id"] == target["material_id"]
    )
    execution = next(
        (
            item
            for item in read["executions"]
            if item.get("execution_id") == observed["execution_id"]
        ),
        None,
    )
    selected_result, selected_snapshot = primary, snapshot
    metadata = {}

    if any(item.get("pdf_row_anchor") for item in field_profile["requirements"]):
        unresolved = {
            item["field_name"]
            for item in snapshot["material_handoff"]["candidates"]
            if item["field"]["status"] != "known"
        }
        page = pdf_row_word_supplement_page(execution, field_profile["requirements"], unresolved)
        word = {"state": "not_triggered"}
        if page is not None:
            word = {
                "state": "not_applied",
                "page": page,
                "primary_result_ref": primary["result_ref"],
                "primary_read_ref": primary["read_ref"],
            }
            try:
                options = read_configurations[("docling", "pdf")]["options"]
                word_configs = pdf_support["configurations"](
                    cache_path=options["cache_path"],
                    declared_versions=reader_versions,
                    pdf_text_cell_unit="word",
                    pdf_page_range=[page, page],
                )
                word_result = once(root.with_name(root.name + "-word"), word_configs, adaptive)
                word_snapshot = read_validated_result(
                    word_result["result_ref"], content_root=content_root
                )
                word.update(
                    word_result_ref=word_result["result_ref"], word_read_ref=word_result["read_ref"]
                )
                previous, updated = _field_states(snapshot), _field_states(word_snapshot)
                unchanged = all(
                    (updated[name]["status"], updated[name]["value"], updated[name].get("unit"))
                    == (field["status"], field["value"], field.get("unit"))
                    for name, field in previous.items()
                    if field["status"] == "known"
                )
                improved = any(updated[name]["status"] == "known" for name in unresolved)
                if unchanged and improved and keeps_conflicts(previous, updated):
                    selected_result, selected_snapshot = word_result, word_snapshot
                    word["state"] = "applied"
                else:
                    word["state"] = (
                        "inconsistent_with_primary" if not unchanged else "still_unresolved"
                    )
            except Exception as error:
                word.update(state="failed", reason=type(error).__name__)
        metadata["word_supplement"] = word

    unresolved = {
        item["field_name"]
        for item in selected_snapshot["material_handoff"]["candidates"]
        if item["field"]["status"] != "known"
    }
    pages = (
        pdf_support["table_supplement_pages"](execution["result"], unresolved) if execution else []
    )
    structure = {"state": "not_triggered", "pages": pages}
    if pages:
        structure.update(
            state="attempted",
            primary_result_ref=primary["result_ref"],
            primary_read_ref=primary["read_ref"],
        )

        def supplement_worker(request):
            return pdf_support["read_pdf_adaptive"](
                request,
                worker_fn=worker_fn,
                structure_pages=pages,
                reuse_result=execution["result"],
                document_timeout=pdf_support["document_timeout"],
                supplement_timeout=pdf_support["supplement_timeout"],
                task_timeout=remaining(),
            )

        table_result = once(
            root.with_name(root.name + "-table"), read_configurations, supplement_worker
        )
        table_snapshot = read_validated_result(
            table_result["result_ref"], content_root=content_root
        )
        table_read = read_result(table_result["read_ref"])
        table_execution = next((item for item in table_read["executions"] if item.get("ref")), None)
        passes = table_execution["result"].get("passes", []) if table_execution else []
        structure["resolved_table_cells"] = (
            table_execution["result"].get("resolved_table_cells", []) if table_execution else []
        )
        if any(
            item.get("termination_reason") == "timeout" or item.get("reason") == "task_timeout"
            for item in passes
        ):
            structure["reason"] = "timeout"
        structure.update(result_ref=table_result["result_ref"], read_ref=table_result["read_ref"])
        before, after = _field_states(selected_snapshot), _field_states(table_snapshot)
        preserved = all(
            name in after
            and (
                (after[name]["status"], after[name]["value"], after[name].get("unit"))
                == (field["status"], field["value"], field.get("unit"))
                or after[name]["status"] == "conflict"
            )
            for name, field in before.items()
            if field["status"] == "known"
        )
        improved = any(after.get(name, {}).get("status") == "known" for name in unresolved)
        conflict = any(after.get(name, {}).get("status") == "conflict" for name in before)
        if preserved and (improved or conflict) and keeps_conflicts(before, after):
            selected_result = table_result
            structure["state"] = "applied"
        else:
            structure["state"] = "still_unresolved" if preserved else "inconsistent_with_primary"
    metadata["pdf_supplement"] = structure
    return {**selected_result, **metadata}
