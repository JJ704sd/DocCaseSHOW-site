"""Assemble local inquiry results from already accepted, source-bound observations.

This is separate from the approved business-result and material-handoff contracts.
The caller confirms the first inquiry and supplies any independently verified system match.
"""

import hashlib
import json
from copy import deepcopy
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .body_objects import _subject_refs
from .derived_fields import verified_density, verified_large_cargo

HEADER_FIELDS = ("subject", "from_addr", "to_addrs", "cc_addrs")
SYSTEM_FIELDS = ("salesperson", "inquiry_created_at", "internal_reference")


def inquiry_identity(email_ids):
    encoded = json.dumps(sorted(email_ids), separators=(",", ":")).encode()
    return "inquiry:" + hashlib.sha256(encoded).hexdigest()[:24]


def cargo_line_identity(email_id, material_id, row):
    location = {key: row[key] for key in ("worksheet", "page", "table", "row") if key in row}
    return row.get("object_id") or (
        "cargo-line:"
        + email_id
        + ":"
        + material_id
        + ":"
        + json.dumps(location, sort_keys=True, ensure_ascii=False)
    )


def project_header_fields(email_id, metadata):
    read = (
        isinstance(metadata, dict)
        and metadata.get("source", {}).get("artifact_kind") == "original_eml"
    )
    fields = {}
    for name in HEADER_FIELDS:
        present = read and name in metadata
        value = deepcopy(metadata[name]) if present else None
        status = (
            "header_not_read"
            if not read
            else "header_unavailable"
            if not present
            else "absent"
            if value is None or value == "" or value == []
            else "known"
        )
        fields[name] = {
            "value": value,
            "status": status,
            "object_id": email_id,
            "unit": None,
            "sources": (
                [{"kind": "eml_header", "email_ref_id": email_id, "field": name}] if present else []
            ),
        }
    return fields


def _system_fields(group_id, supplied):
    supplied = supplied or {"state": "not_connected"}
    if not isinstance(supplied, dict):
        supplied = {"state": "query_failed"}
    state = supplied.get("state", "not_connected")
    matches = supplied.get("matches", []) if state == "ok" else []
    record = None
    if state == "ok":
        if not isinstance(matches, list):
            state = "query_failed"
        elif len(matches) == 0:
            state = "no_match"
        elif len(matches) != 1:
            state = "multiple_matches"
        else:
            candidate = matches[0]
            source = candidate.get("source", {}) if isinstance(candidate, dict) else {}
            if (
                isinstance(candidate, dict)
                and isinstance(source, dict)
                and candidate.get("verified") is True
                and candidate.get("group_id") == group_id
                and candidate.get("inquiry_id")
                and isinstance(candidate.get("fields"), dict)
                and source.get("kind") == "business_system"
                and source.get("ref")
            ):
                state, record = "matched", candidate
            else:
                state = "match_unverified"
    if state not in {
        "matched",
        "no_match",
        "multiple_matches",
        "query_failed",
        "not_connected",
        "match_unverified",
    }:
        state = "query_failed"
    fields = {}
    for name in SYSTEM_FIELDS:
        value = record["fields"].get(name) if record else None
        present = value is not None and value != ""
        fields[name] = {
            "value": value if present else None,
            "status": "known" if present else "missing_in_record" if record else state,
            "object_id": record["inquiry_id"] if record else group_id,
            "unit": None,
            "sources": [{**record["source"], "field": name}] if present else [],
        }
    return fields


def _fact(field, name, scope_id, kind, email_id, material_id, read_material_id=None):
    if not isinstance(field, dict):
        return None
    sources = [
        {
            **source,
            "email_ref_id": email_id,
            "material_id": material_id,
            **({"read_material_id": read_material_id} if read_material_id else {}),
        }
        for source in field.get("sources", [])
        if isinstance(source, dict)
    ]
    # PDF packing_rows are produced only after local proof; older PDF entries omit status.
    status = field.get(
        "status", "known" if field.get("value") is not None and sources else "uncertain"
    )
    value = field.get("value")
    if name == "line_volume" and kind == "cargo_line" and status == "known" and sources:
        source = sources[0]
        if source.get("sheet") and source.get("cell") and source.get("excerpt") is not None:
            try:
                exact, visible = Decimal(str(source["excerpt"])), Decimal(str(value))
                if (
                    exact.is_finite()
                    and visible.is_finite()
                    and exact > 0
                    and visible > 0
                    and exact.quantize(
                        Decimal(1).scaleb(visible.as_tuple().exponent), rounding=ROUND_HALF_UP
                    )
                    == visible
                ):
                    value = float(exact)
            except (InvalidOperation, ValueError):
                pass
    return {
        "value": value,
        "unit": field.get("unit"),
        "status": status,
        "scope_id": scope_id,
        "object_kind": kind,
        "sources": sources,
        "field_name": name,
    }


def _measurements(fields, scope_id, email_id, material_id, kind, read_material_id=None):
    by_name = {}
    for field in fields:
        name = field.get("name")
        if name in by_name:
            old = by_name[name]
            if old.get("status") == "conflict" or field.get("status") == "conflict":
                by_name[name] = {"status": "conflict"}
            elif old.get("status") != "known":
                by_name[name] = field
            elif field.get("status") == "known":
                if (old.get("value"), old.get("unit")) == (field.get("value"), field.get("unit")):
                    by_name[name] = {
                        **old,
                        "sources": old.get("sources", []) + field.get("sources", []),
                    }
                else:
                    by_name[name] = {"status": "conflict"}
        elif name:
            by_name[name] = field
    gross_name = "shipment_gross_weight" if kind == "shipment" else "line_gross_weight"
    gross = _fact(
        by_name.get(gross_name), gross_name, scope_id, kind, email_id, material_id, read_material_id
    )
    volume = _fact(
        by_name.get("line_volume"),
        "line_volume",
        scope_id,
        kind,
        email_id,
        material_id,
        read_material_id,
    )
    charge = (
        _fact(
            by_name.get("shipment_chargeable_weight"),
            "shipment_chargeable_weight",
            scope_id,
            kind,
            email_id,
            material_id,
            read_material_id,
        )
        if kind == "shipment"
        else None
    )
    density = verified_density(gross, volume)
    density["object_id"] = scope_id
    if kind == "cargo_line":
        return {"cargo_line_density": density}
    large = verified_large_cargo(charge)
    large["object_id"] = scope_id
    return {"shipment_density": density, "is_large_cargo": large}


def _first_direct(group, first_id):
    result = []
    for field in group.get("group_fields", []):
        entry = {
            "name": field["name"],
            "value": None,
            "unit": field.get("unit"),
            "sources": [],
            "object_id": first_id,
            "status": "unconfirmed_first_inquiry" if first_id is None else "uncertain",
        }
        if first_id and field.get("status") == "conflict":
            entry["status"] = "conflict"
            entry["sources"] = [
                {
                    **source,
                    "email_ref_id": first_id,
                    "material_id": source.get("material_id", candidate.get("material_id")),
                }
                for candidate in field.get("body_entries", [])
                if candidate.get("email_id") == first_id
                for source in candidate.get("sources", [])
            ]
        elif first_id:
            candidates = [
                item
                for item in field.get("body_entries", [])
                if item.get("email_id") == first_id
                and item.get("status") == "known"
                and item.get("value") is not None
                and item.get("sources")
            ]
            if len(candidates) == 1:
                candidate = candidates[0]
                entry.update(
                    value=candidate["value"],
                    unit=candidate.get("unit"),
                    status="known",
                    sources=[
                        {
                            **source,
                            "email_ref_id": first_id,
                            "material_id": source.get("material_id", candidate.get("material_id")),
                        }
                        for source in candidate["sources"]
                    ],
                )
        result.append(entry)
    return result


def _bind_body_sources(fields, email_id, material_id, read_material_id):
    result = deepcopy(fields)
    for field in result.values():
        field["sources"] = [
            {
                **source,
                "email_ref_id": email_id,
                "material_id": material_id,
                **({"read_material_id": read_material_id} if read_material_id else {}),
            }
            for source in field.get("sources", [])
        ]
    return result


def _deduplicate_quoted_tables(shipments, cargo_lines):
    """Coalesce complete identical quoted tables only when the reference also matches."""
    by_shipment = {}
    for line in cargo_lines:
        by_shipment.setdefault(line.get("shipment_id"), []).append(line)
    names = ("line_net_weight", "line_gross_weight", "line_volume")
    signatures = {}
    for shipment in shipments:
        rows = by_shipment.get(shipment["object_id"], [])
        if shipment.get("source_role") != "historical_quote" or not rows:
            continue
        if any(
            not row.get("row_evidence_hash")
            or any(name not in row["direct_fields"] for name in names)
            for row in rows
        ):
            continue
        signatures[shipment["object_id"]] = tuple(
            (
                row["row_evidence_hash"],
                tuple(
                    (row["direct_fields"][name]["value"], row["direct_fields"][name]["unit"])
                    for name in names
                ),
            )
            for row in rows
        )
    removed_shipments, removed_rows = set(), set()
    for duplicate in shipments:
        sid = duplicate["object_id"]
        if sid not in signatures or sid in removed_shipments:
            continue
        matching = [
            canonical
            for canonical in shipments
            if canonical["object_id"] != sid
            and canonical["object_id"] not in removed_shipments
            and signatures.get(canonical["object_id"]) == signatures[sid]
            and canonical.get("association", {}).get("status") == "verified"
            and canonical["association"].get("reference") in duplicate.get("subject_references", [])
        ]
        if len(matching) != 1:
            continue
        canonical = matching[0]
        # Preserve the first verified occurrence as the stable projected object.
        if shipments.index(canonical) > shipments.index(duplicate):
            continue
        canonical.setdefault("duplicate_evidence", []).append(
            {
                "email_ref_id": duplicate["email_ref_id"],
                "material_id": duplicate["material_id"],
                "object_id": sid,
                "location": duplicate["location"],
                "basis": "identical_table_and_subject_reference",
            }
        )
        for original, copied in zip(
            by_shipment[canonical["object_id"]], by_shipment[sid], strict=True
        ):
            for name in names:
                original["direct_fields"][name]["sources"].extend(
                    copied["direct_fields"][name]["sources"]
                )
            removed_rows.add(copied["object_id"])
        removed_shipments.add(sid)
    return (
        [s for s in shipments if s["object_id"] not in removed_shipments],
        [line for line in cargo_lines if line["object_id"] not in removed_rows],
    )


def first_inquiry_status(confirmed_email_id, archive_evidence=None):
    """Keep archive availability separate from a confirmed email identity."""
    evidence = archive_evidence if isinstance(archive_evidence, dict) else {}
    available = evidence.get("original_first_inquiry_available")
    availability = (
        "available_in_archive"
        if available is True
        else "not_available_in_archive"
        if available is False
        else "unknown"
    )
    confirmed = confirmed_email_id is not None
    return {
        "original_availability": availability,
        "confirmation": "confirmed" if confirmed else "unconfirmed",
        "confirmation_basis": "manual_selection" if confirmed else None,
        "reason": None
        if confirmed
        else (
            "original_not_in_archive" if available is False else "first_email_identity_unconfirmed"
        ),
        "archive_evidence": deepcopy(evidence) if evidence else None,
    }


def build_inquiry_result(
    group,
    jobs,
    *,
    confirmed_first_email_id=None,
    system_result=None,
    context_supplements=(),
    first_inquiry_evidence=None,
):
    """Return one local, versioned result for the workbench and JSON export."""
    ids = [job["id"] for job in jobs]
    if confirmed_first_email_id is not None and confirmed_first_email_id not in ids:
        raise ValueError("confirmed_first_inquiry_not_in_group")
    inquiry_id = inquiry_identity(ids)
    first_job = next((job for job in jobs if job["id"] == confirmed_first_email_id), None)
    first_metadata = next(
        (
            item
            for item in (first_job or {}).get("metadata", [])
            if item.get("email_ref_id") == confirmed_first_email_id
        ),
        {},
    )
    first_subject = first_metadata.get("subject") or ""
    first_refs = _subject_refs(first_subject)
    first_body = [m for m in (first_job or {}).get("materials", []) if m.get("kind") == "body"]
    multi_first = (
        len(first_body) == 1
        and sum(
            s.get("source_role") == "current_body" for s in first_body[0].get("body_shipments", [])
        )
        > 1
    )
    emails = []
    supplements, cargo_lines, shipments = [], [], []
    main = None
    for job in jobs:
        email_id = job["id"]
        metadata = next(
            (item for item in job.get("metadata", []) if item.get("email_ref_id") == email_id), {}
        )
        headers = project_header_fields(email_id, metadata)
        emails.append({"email_ref_id": email_id, "fields": headers})
        body_count = sum(m.get("kind") == "body" for m in job.get("materials", []))
        for material in job.get("materials", []):
            material_id = material["id"]
            read_material_id = material.get("read_material_id")
            scope = f"shipment:{email_id}:{material_id}"
            read_bound = material.get("read_bound") is True
            fields = material.get("fields", []) if read_bound else []
            role = (
                "first_inquiry"
                if email_id == confirmed_first_email_id
                and material.get("kind") == "body"
                and body_count == 1
                else "reference"
            )
            derived = _measurements(
                fields, scope, email_id, material_id, "shipment", read_material_id
            )
            if not read_bound:
                for value in derived.values():
                    value["reason"] = "read_not_verified"
            scope_result = {
                "object_id": scope,
                "email_ref_id": email_id,
                "material_id": material_id,
                "read_material_id": read_material_id,
                "filename": material.get("filename"),
                "role": role,
                "fields": derived,
            }
            if role == "first_inquiry" and main is None:
                main = scope_result
            elif material.get("kind") in {"body", "attachment", "inline"}:
                supplements.append(scope_result)
            if material.get("kind") == "body" and read_bound:
                for body_shipment in material.get("body_shipments", []):
                    source_role = body_shipment.get("source_role", "unknown")
                    association = deepcopy(body_shipment.get("association", {}))
                    same_ticket = (
                        association.get("status") == "verified"
                        and association.get("reference") in first_refs
                    )
                    if same_ticket:
                        association["group_binding"] = {
                            "basis": "first_inquiry_subject_reference",
                            "first_email_ref_id": confirmed_first_email_id,
                            "subject": first_subject,
                            "scope": "shipment_identifier_only",
                        }
                    body_role = (
                        "historical_quote"
                        if source_role == "historical_quote"
                        else "first_inquiry"
                        if role == "first_inquiry" and source_role == "current_body"
                        else "supplement"
                        if same_ticket
                        else "reference"
                    )
                    shipments.append(
                        {
                            **deepcopy(body_shipment),
                            "email_ref_id": email_id,
                            "material_id": material_id,
                            "read_material_id": read_material_id,
                            "filename": material.get("filename"),
                            "role": body_role,
                            "association": association,
                            "direct_fields": _bind_body_sources(
                                body_shipment.get("direct_fields", {}),
                                email_id,
                                material_id,
                                read_material_id,
                            ),
                        }
                    )
            rows = (
                material.get("packing_rows", [])
                if material.get("kind") != "body" or read_bound
                else []
            )
            for row in rows:
                location = {k: row[k] for k in ("worksheet", "page", "table", "row") if k in row}
                row_id = cargo_line_identity(email_id, material_id, row)
                direct_fields = (
                    _bind_body_sources(
                        row.get("fields", {}), email_id, material_id, read_material_id
                    )
                    if material.get("kind") == "body"
                    else deepcopy(row.get("fields", {}))
                )
                row_fields = [{"name": name, **item} for name, item in direct_fields.items()]
                cargo_lines.append(
                    {
                        "object_id": row_id,
                        "email_ref_id": email_id,
                        "material_id": material_id,
                        "read_material_id": read_material_id,
                        "filename": material.get("filename"),
                        "role": "cargo_line",
                        "location": location,
                        "ordinal": row.get("ordinal"),
                        "direct_fields": direct_fields,
                        **(
                            {
                                k: deepcopy(row[k])
                                for k in (
                                    "shipment_id",
                                    "source_role",
                                    "association",
                                    "row_evidence_hash",
                                )
                                if k in row
                            }
                        ),
                        "fields": _measurements(
                            row_fields,
                            row_id,
                            email_id,
                            material_id,
                            "cargo_line",
                            read_material_id,
                        ),
                    }
                )
    # Group-context facts are separate supplements. They may supplement other
    # accepted observations from the same material, never the first inquiry.
    for supplement in context_supplements:
        email_id, material_id = supplement.get("email_id"), supplement.get("material_id")
        if (
            email_id not in ids
            or not supplement.get("sources")
            or supplement.get("role") != "supplement"
        ):
            continue
        target = next(
            (
                s
                for s in supplements
                if s["email_ref_id"] == email_id and s["material_id"] == material_id
            ),
            None,
        )
        job = next(j for j in jobs if j["id"] == email_id)
        material = next((m for m in job.get("materials", []) if m["id"] == material_id), None)
        if material:
            if target is None:
                target = {
                    "object_id": f"shipment:{email_id}:{material_id}:context",
                    "email_ref_id": email_id,
                    "material_id": material_id,
                    "role": "reference",
                    "fields": {},
                }
                supplements.append(target)
            if (
                email_id == confirmed_first_email_id
                and len(first_body) == 1
                and material_id == first_body[0]["id"]
                and not multi_first
            ):
                target["role"] = "supplement"
            related = [
                s
                for s in context_supplements
                if s.get("email_id") == email_id
                and s.get("material_id") == material_id
                and s.get("role") == "supplement"
                and s.get("sources")
            ]
            fields = list(material.get("fields", []))
            fields += [{**s, "status": "known"} for s in related]
            target["fields"] = _measurements(
                fields,
                target["object_id"],
                email_id,
                material_id,
                "shipment",
                material.get("read_material_id"),
            )
    shipments, cargo_lines = _deduplicate_quoted_tables(shipments, cargo_lines)
    first_headers = next(
        (e["fields"] for e in emails if e["email_ref_id"] == confirmed_first_email_id), None
    )
    if first_headers is None:
        first_headers = {
            name: {
                "value": None,
                "status": "unconfirmed_first_inquiry",
                "object_id": inquiry_id,
                "unit": None,
                "sources": [],
            }
            for name in HEADER_FIELDS
        }
    if main is None:
        scope = f"shipment:{inquiry_id}:unconfirmed"
        main = {
            "object_id": scope,
            "email_ref_id": None,
            "material_id": None,
            "role": "first_inquiry",
            "fields": _measurements([], scope, None, None, "shipment"),
        }
        for item in main["fields"].values():
            item["reason"] = (
                "first_inquiry_unconfirmed"
                if confirmed_first_email_id is None
                else "first_inquiry_body_missing"
            )
    first_status = first_inquiry_status(confirmed_first_email_id, first_inquiry_evidence)
    if confirmed_first_email_id is not None:
        confirmed_job = next(job for job in jobs if job["id"] == confirmed_first_email_id)
        bodies = [
            material
            for material in confirmed_job.get("materials", [])
            if material.get("kind") == "body"
        ]
        first_status["selected_email_content"] = (
            "available" if confirmed_job.get("email_sha256") else "unknown"
        )
        first_status["body_read_state"] = (
            "missing"
            if len(bodies) != 1
            else "verified"
            if bodies[0].get("read_bound") is True
            else "unverified"
        )
        if first_status["body_read_state"] != "verified":
            first_status["reason"] = "first_inquiry_body_" + first_status["body_read_state"]
    else:
        first_status.update(selected_email_content="unknown", body_read_state="not_applicable")
    return {
        "format": "doccase-local-inquiry/1",
        "inquiry_id": inquiry_id,
        "emails": emails,
        "first_inquiry": {
            "email_ref_id": confirmed_first_email_id,
            "source_status": first_status,
            "email_headers": deepcopy(first_headers),
            "direct_fields": _first_direct(group, confirmed_first_email_id),
        },
        "shipment": main,
        "shipments": shipments,
        "supplements": supplements,
        "cargo_lines": cargo_lines,
        "business_system": _system_fields(inquiry_id, system_result),
    }
