"""Compare explicitly selected local email and PDF material results."""

import argparse
import json
import re
import sys
from decimal import Decimal
from pathlib import Path

from local_parse_server import _response

from doccase_preprocessing.derived_fields import density_kg_per_m3, is_large_cargo
from doccase_preprocessing.main_channel import read_result, read_validated_result

COMPARISON_FIELDS = (
    ("package_count", None),
    ("line_gross_weight", "kg"),
    ("line_volume", "CBM"),
)


def _known_field(response, name, unit):
    fields = [field for field in response["fields"] if field["field_name"] == name]
    if len(fields) != 1 or fields[0]["status"] != "known" or fields[0]["unit"] != unit:
        return None
    value = fields[0]["value"]
    return (
        Decimal(str(value))
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else None
    )


def _observation_comparison(body, documents):
    terms = []
    for field in body["fields"]:
        if field["field_name"] == "trade_terms":
            for source in field["sources"]:
                excerpt = source["source"]["excerpt"]
                match = re.search(
                    r"\b(?:EXW|FCA|FAS|FOB|CFR|CIF|CPT|CIP|DAP|DPU|DDP)\b.*", excerpt, re.I
                )
                if match:
                    terms.append(
                        {"run_id": body["run_id"], "value": match.group().strip(), "source": source}
                    )
    shipping = []
    for document in documents:
        for item in document.get("document_observations", []):
            observation = {
                "run_id": document["run_id"],
                "value": item["value"],
                "source": item["source"],
            }
            if item["field_name"] == "shipment_terms":
                terms.append(observation)
            elif item["field_name"] == "shipping_way":
                shipping.append(observation)
    return {
        name: {
            "observations": items,
            "distinct_values_need_review": len({item["value"].casefold() for item in items}) > 1,
        }
        for name, items in (("shipment_terms", terms), ("shipping_way", shipping))
    }


def compare_material_totals(body, documents, *, include_identical_documents=False):
    """Compare trusted, explicitly selected material occurrences from one EML."""
    if not documents:
        raise ValueError("Select at least one attachment")
    email = body.get("email_sha256")
    if not email or any(item.get("email_sha256") != email for item in documents):
        raise ValueError("Body and selected attachments must originate from the same original EML")
    identities = [item.get("mime_path") for item in documents]
    if not all(identities):
        raise ValueError("Missing attachment occurrence")
    if len(set(identities)) != len(identities):
        raise ValueError("Duplicate attachment occurrence")
    by_content = {}
    for item in documents:
        digest = item.get("attachment_sha256")
        if digest:
            by_content.setdefault(digest, []).append(item["id"])
    duplicates = [
        {"sha256": digest, "run_ids": run_ids}
        for digest, run_ids in by_content.items()
        if len(run_ids) > 1
    ]
    totals = {}
    for name, unit in COMPARISON_FIELDS:
        body_value = _known_field(body, name, unit)
        values = [_known_field(item, name, unit) for item in documents]
        total = (
            sum(values, Decimal(0))
            if all(value is not None for value in values)
            and (not duplicates or include_identical_documents)
            else None
        )
        comparison = (
            "unresolved"
            if body_value is None or total is None
            else "match"
            if body_value == total
            else "different"
        )
        totals[name] = {
            "unit": unit,
            "body_value": float(body_value) if body_value is not None else None,
            "selected_document_sum": float(total) if total is not None else None,
            "document_values": [float(value) if value is not None else None for value in values],
            "comparison": comparison,
        }
    return totals, duplicates


def compare_selected(
    body, documents, *, chargeable_weight_kg=None, include_identical_documents=False
):
    def occurrence(item):
        return {**item["indexed_source"], "id": item["run_id"], "fields": item["fields"]}

    totals, duplicates = compare_material_totals(
        occurrence(body),
        [occurrence(item) for item in documents],
        include_identical_documents=include_identical_documents,
    )
    chargeable_fields = [
        field for field in body["fields"] if field["field_name"] == "chargeable_weight"
    ]
    chargeable = _known_field(body, "chargeable_weight", "kg")
    supplied = Decimal(str(chargeable_weight_kg)) if chargeable_weight_kg is not None else None
    if supplied is not None and (not supplied.is_finite() or supplied < 0):
        raise ValueError("Chargeable weight must be a nonnegative finite kg value")
    conflicting_weight = any(item["status"] == "conflict" for item in chargeable_fields) or (
        chargeable is not None and supplied is not None and chargeable != supplied
    )
    if conflicting_weight:
        chargeable = None
    elif chargeable is None:
        chargeable = supplied
    return {
        "schema": "local-selected-material-comparison/0.1",
        "status": "partial",
        "scope": "caller_selected_materials",
        "selection_complete": None,
        "business_group_confirmed": False,
        "possible_duplicate_documents": duplicates,
        "include_identical_documents": include_identical_documents,
        "body": {
            "run_id": body["run_id"],
            "email_id": body["email_id"],
            "result_ref": body["result_ref"],
        },
        "documents": [
            {
                "run_id": item["run_id"],
                "email_id": item["email_id"],
                "attachment_id": item["indexed_source"]["attachment_id"],
                "result_ref": item["result_ref"],
            }
            for item in documents
        ],
        "totals": totals,
        "chargeable_weight": {
            "value": float(chargeable) if chargeable is not None else None,
            "unit": "kg",
            "status": "known"
            if chargeable is not None
            else "conflict"
            if conflicting_weight
            else "uncertain",
            "run_id": body["run_id"],
            "caller_input_kg": float(supplied) if supplied is not None else None,
            "sources": [source for item in chargeable_fields for source in item["sources"]],
        },
        "derived": {
            "density_kg_per_m3": density_kg_per_m3(
                totals["line_gross_weight"]["selected_document_sum"],
                totals["line_volume"]["selected_document_sum"],
            ),
            "is_large_cargo": is_large_cargo(chargeable),
        },
        "observations": _observation_comparison(body, documents),
    }


def load_response(path):
    """Rebuild the local response from validated material and READ snapshots."""
    path = Path(path).resolve(strict=True)
    saved = json.loads(path.read_text(encoding="utf-8"))
    snapshot = read_validated_result(saved["result_ref"], content_root=path.parent / "p01")
    handoff = snapshot["material_handoff"]
    if saved["read_ref"] != snapshot["read_result_ref"]:
        raise ValueError("READ reference does not match the material snapshot")
    read = read_result(saved["read_ref"])
    manifest = read_result(handoff["input_manifest_ref"])
    profile_id = handoff["required_field_profile"]["id"]
    email_id = manifest["emails"][0]["email_ref_id"]
    if saved["profile_id"] != profile_id or saved["email_id"] != email_id:
        raise ValueError("Saved response does not match its material snapshot")
    source = saved["indexed_source"]
    original = next(item for item in manifest["materials"] if item["kind"] == "original_email")
    if original["content"]["sha256"] != source["email_sha256"]:
        raise ValueError("Email source does not match the manifest")
    if "attachment_id" in source:
        mid = handoff["required_field_profile"]["requirements"][0]["material_id"]
        target = next(item for item in manifest["materials"] if item["material_id"] == mid)
        if (
            target["metadata"].get("mime_path") != source["mime_path"]
            or target["content"]["sha256"] != source["attachment_sha256"]
        ):
            raise ValueError("Attachment source does not match the manifest")
    rebuilt = _response(
        email_id,
        profile_id,
        saved["run_id"],
        {"result_ref": saved["result_ref"], "read_ref": saved["read_ref"]},
        snapshot,
        read,
        manifest,
    )
    rebuilt["indexed_source"] = source
    return rebuilt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--body", required=True, type=Path, help="body response.json")
    parser.add_argument(
        "--pdf",
        required=True,
        type=Path,
        action="append",
        help="PDF response.json; repeat for each document",
    )
    parser.add_argument("--output", type=Path, help="new comparison JSON file; defaults to stdout")
    parser.add_argument(
        "--chargeable-weight-kg", type=Decimal, help="caller-supplied chargeable weight in kg"
    )
    parser.add_argument(
        "--include-identical-documents",
        action="store_true",
        help="include identical PDF bytes after the caller confirms distinct documents",
    )
    args = parser.parse_args()
    body = load_response(args.body)
    documents = [load_response(path) for path in args.pdf]
    if body["profile_id"] not in {"local-current-body-totals", "local-current-body-inquiry"}:
        parser.error("--body must select a current-body response")
    if any(
        item["profile_id"] not in {"indexed-pdf-document", "indexed-pdf-document-cargo"}
        for item in documents
    ):
        parser.error("--pdf must select document-scope PDF responses")
    result = compare_selected(
        body,
        documents,
        chargeable_weight_kg=args.chargeable_weight_kg,
        include_identical_documents=args.include_identical_documents,
    )
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        with args.output.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
    else:
        sys.stdout.buffer.write(encoded.encode("utf-8"))


if __name__ == "__main__":
    main()
