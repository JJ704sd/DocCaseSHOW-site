"""Read-only review of explicitly selected EML jobs. No cross-mail totals."""

import json


def _body_field(name, emails, bodies):
    entries = []
    for mail, body in zip(emails, bodies, strict=True):
        matches = [f for f in body["fields"] if f["name"] == name] if body else []
        for field in matches or [None]:
            entries.append(
                {
                    "email_id": mail["id"],
                    "material_id": body["id"] if body else None,
                    "value": field["value"] if field else None,
                    "unit": field["unit"] if field else None,
                    "status": field["status"] if field else "uncertain",
                    "reason": field.get("reason") if field else (body or mail).get("reason"),
                    "sources": field["sources"] if field else [],
                    "via": field.get("via") if field else None,
                }
            )
    known = [
        e for e in entries if e["status"] == "known" and e["value"] is not None and e["sources"]
    ]
    distinct = []
    for entry in known:
        key = (entry["value"], entry["unit"])
        if key not in distinct:
            distinct.append(key)
    conflict = len(distinct) > 1 or any(e["status"] == "conflict" for e in entries)
    current_known = any(e["email_id"] == emails[0]["id"] for e in known)
    seen = []
    for entry in entries:
        key = (entry["value"], entry["unit"])
        if entry["status"] == "conflict" or (conflict and entry in known):
            relation = "conflict"
        elif entry not in known:
            relation = "uncertain"
        elif entry["email_id"] == emails[0]["id"]:
            relation = "current"
        elif key in seen:
            relation = "duplicate"
        else:
            relation = "supplement"
        entry["relationship"] = relation
        if entry in known:
            seen.append(key)
    status = (
        "conflict"
        if conflict
        else "uncertain"
        if not known
        else "supplement"
        if not current_known
        else "duplicate"
        if any(e["relationship"] == "duplicate" for e in entries)
        else "current"
    )
    return {"name": name, "status": status, "entries": entries}


def _group_fields(body_fields, emails):
    """Project evidence across selected messages without merging distinct document scopes."""
    documents = {}
    for mail in emails:
        for item in mail["attachments"]:
            for field in item["fields"]:
                if not field["sources"] and field["status"] != "conflict":
                    continue
                documents.setdefault(field["name"], []).append(
                    {
                        **field,
                        "email_id": mail["id"],
                        "material_id": item["id"],
                        "duplicate_of": item["duplicate_of"],
                        "relationship": item["relationship"],
                    }
                )
    body_map = {field["name"]: field for field in body_fields}
    result = []
    for name in dict.fromkeys([*body_map, *documents]):
        body = body_map.get(name, {"status": "uncertain", "entries": []})
        attachments = documents.get(name, [])
        status = body["status"]
        reference = (
            next((e for e in body["entries"] if e["relationship"] == "current"), None)
            if status in {"current", "duplicate"}
            else None
        )
        if status == "uncertain" and any(
            f["status"] == "known" and f["value"] is not None for f in attachments
        ):
            status = "supplement"
        values = []
        for scope, entries in (("body", body["entries"]), ("attachment", attachments)):
            for entry in entries:
                if entry["status"] != "known" or entry["value"] is None or not entry["sources"]:
                    continue
                candidate = {"scope": scope, "value": entry["value"], "unit": entry["unit"]}
                if candidate not in values:
                    values.append(candidate)
        result.append(
            {
                "name": name,
                "status": status,
                "value": reference["value"] if reference else None,
                "unit": reference["unit"] if reference else None,
                "candidates": values,
                "body_entries": body["entries"],
                "attachment_entries": attachments,
            }
        )
    return result


def _duplicate(seen, digest, origin):
    if not digest:
        return None
    if digest in seen:
        return seen[digest]
    seen[digest] = origin
    return None


def compare_group_routes(rules, combined):
    """Compare both validated views; outcome counts are not ground-truth scores."""

    def summary(field):
        if not field:
            return (
                {
                    "status": "uncertain",
                    "value": None,
                    "unit": None,
                    "candidates": [],
                    "sources": 0,
                },
                set(),
                set(),
            )
        evidence = set()
        for key in ("body_entries", "attachment_entries"):
            for entry in field[key]:
                for source in entry["sources"]:
                    evidence.add(
                        json.dumps(
                            (
                                key,
                                entry["email_id"],
                                entry["material_id"],
                                {
                                    locator: source[locator]
                                    for locator in (
                                        "pointer",
                                        "sheet",
                                        "cell",
                                        "line",
                                        "table",
                                        "row",
                                        "page",
                                        "column",
                                        "excerpt",
                                        "start",
                                    )
                                    if source.get(locator) is not None
                                },
                            ),
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                    )
        candidates = {(item["scope"], item["value"], item["unit"]) for item in field["candidates"]}
        return (
            {
                **{key: field[key] for key in ("status", "value", "unit", "candidates")},
                "sources": len(evidence),
            },
            evidence,
            candidates,
        )

    before = {field["name"]: field for field in rules["group_fields"]}
    after = {field["name"]: field for field in combined["group_fields"]}
    changes = []
    for name in dict.fromkeys([*before, *after]):
        old, old_sources, old_candidates = summary(before.get(name))
        new, new_sources, new_candidates = summary(after.get(name))
        main = (old["value"], old["unit"]) != (new["value"], new["unit"])
        state = old["status"] != new["status"]
        if main or state or old_sources != new_sources or old_candidates != new_candidates:
            changes.append(
                {"name": name, "group_value_changed": main, "rules": old, "combined": new}
            )
    return {
        "scope": "same_read",
        "group_value_changes": sum(item["group_value_changed"] for item in changes),
        "other_changes": sum(not item["group_value_changed"] for item in changes),
        "changes": changes,
    }


def build_group_review(jobs, body_names):
    emails, bodies, seen_mail, seen_attachment = [], [], {}, {}
    for index, job in enumerate(jobs):
        related = index > 0
        metadata = next(iter(job["metadata"]), {})
        row = {
            "id": job["id"],
            "filename": job["filename"],
            "subject": metadata.get("subject"),
            "sent_at": metadata.get("sent_at"),
            "state": job["state"],
            "reason": job.get("reason"),
            "parse_scope": job["parse_scope"],
            "route": job.get("route"),
            "role": "related" if related else "current",
            "duplicate_of": _duplicate(seen_mail, job["email_sha256"], job["id"]),
            "attachments": [],
        }
        body_rows = [r for r in job["materials"] if r["kind"] == "body"]
        body = body_rows[0] if len(body_rows) == 1 else None
        row["body"] = (
            {k: v for k, v in body.items() if k not in {"fields", "sha256"}} if body else None
        )
        if len(body_rows) > 1:
            row["reason"] = "ambiguous_body"
        for item in job["materials"]:
            if item["kind"] not in {"attachment", "inline"}:
                continue
            origin = {"email_id": job["id"], "material_id": item["id"]}
            match = _duplicate(seen_attachment, item.get("sha256"), origin)
            relation = "duplicate" if match else "supplement" if related else "current"
            row["attachments"].append(
                {
                    **{k: v for k, v in item.items() if k != "sha256"},
                    "duplicate_of": match,
                    "relationship": relation,
                }
            )
        emails.append(row)
        bodies.append(body)
    names = dict.fromkeys(
        [*body_names, *(field["name"] for body in bodies if body for field in body["fields"])]
    )
    body_fields = [_body_field(name, emails, bodies) for name in names]
    return {
        "scope": "manually_selected_emails",
        "current_email_id": emails[0]["id"],
        "emails": emails,
        "body_fields": body_fields,
        "group_fields": _group_fields(body_fields, emails),
    }
