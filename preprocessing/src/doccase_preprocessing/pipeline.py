"""Preserve material occurrences and prepare traceable downstream parsing work."""

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any
from uuid import uuid4

from .inspection import inspect_material
from .models import Bundle, InputError, Issue, Limits, MaterialError, ParserConfig


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")


def _store_content(root: Path, raw: bytes) -> dict[str, Any]:
    digest = _digest(raw)
    relative = Path("objects") / digest
    target = root / relative
    temporary = target.with_name(f".{digest}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(raw)
        try:
            # Publish complete bytes atomically without overwriting an existing object.
            target.hardlink_to(temporary)
        except FileExistsError as error:
            # Reusing a corrupted original would make provenance and future parsing false.
            if _digest(target.read_bytes()) != digest:
                raise ValueError(f"Stored original hash mismatch: {relative.as_posix()}") from error
    finally:
        temporary.unlink(missing_ok=True)
    return {"path": relative.as_posix(), "sha256": digest, "size_bytes": len(raw)}


def prepare(
    bundle: Bundle,
    output_dir: str | Path,
    *,
    limits: Limits | None = None,
    parser_config: ParserConfig | None = None,
    fetch_attachment: Callable[[str, int], bytes] | None = None,
) -> dict[str, Any]:
    """Write originals and one immutable handoff manifest; no parser/model is executed.

    fetch_attachment is an upstream-owned authorized resolver: (locator, byte_limit) -> bytes.
    It must enforce authorization, destination, timeout and streaming size constraints, and
    raise MaterialError for known expiry/permission/timeout failures. Unexpected failures
    propagate. No arbitrary URL fetch, credential storage, retry or alternate provider exists here.
    """
    limits = limits if limits is not None else Limits()
    parser_config = parser_config if parser_config is not None else ParserConfig()
    if len(bundle.materials) > limits.max_materials:
        raise InputError(f"Material count exceeds {limits.max_materials}")
    ids = [material.material_id for material in bundle.materials]
    if len(ids) != len(set(ids)):
        raise InputError("Duplicate material_id in bundle")

    root = Path(output_dir)
    (root / "objects").mkdir(parents=True, exist_ok=True)
    (root / "manifests").mkdir(exist_ok=True)
    records: list[dict[str, Any]] = []
    tasks: dict[str, dict[str, Any]] = {}
    total_bytes = 0

    for material in bundle.materials:
        record: dict[str, Any] = {
            "material_id": material.material_id,
            "email_ref_id": material.email_ref_id,
            "role": material.role,
            "kind": material.kind,
            "filename": material.filename,
            "declared_mime": material.mime,
            "cid": material.cid,
            "alternative_group": material.alternative_group,
            "parent_material_id": material.parent_material_id,
            "selected_for_parsing": material.selected,
            "metadata": material.metadata,
            "representation": material.representation,
            "content": None,
            "original": None,
            "format": None,
            "scope": None,
            "parse_task_id": None,
            "state": "blocked",
            "issues": [asdict(issue) for issue in material.issues],
        }
        records.append(record)
        try:
            raw = material.content
            if raw is None and material.locator and not material.issues:
                remaining_bytes = limits.max_input_bytes - total_bytes
                if remaining_bytes <= 0:
                    raise MaterialError(
                        "limit_exceeded",
                        "Total material byte limit exhausted before acquisition",
                        actual=total_bytes,
                        limit=limits.max_input_bytes,
                        resource="total_bytes",
                    )
                if fetch_attachment is None:
                    raise MaterialError(
                        "acquisition_not_configured",
                        "Authorized attachment resolver is required; content was not acquired",
                    )
                raw = fetch_attachment(
                    material.locator, min(limits.max_material_bytes, remaining_bytes)
                )
                if not isinstance(raw, bytes):
                    raise TypeError("Authorized attachment resolver must return bytes")
            if raw is None:
                if not record["issues"]:
                    record["issues"].append(
                        asdict(Issue("unavailable", "Content was not provided"))
                    )
                continue
            if len(raw) > limits.max_material_bytes:
                raise MaterialError(
                    "limit_exceeded",
                    "Material byte limit exceeded",
                    actual=len(raw),
                    limit=limits.max_material_bytes,
                    resource="material_bytes",
                )
            # EML originals contain the decoded children; do not count the container twice.
            if material.kind != "original_email":
                total_bytes += len(raw)
                if total_bytes > limits.max_input_bytes:
                    raise MaterialError(
                        "limit_exceeded",
                        "Total material byte limit exceeded",
                        actual=total_bytes,
                        limit=limits.max_input_bytes,
                        resource="total_bytes",
                    )
            record["content"] = _store_content(root, raw)
            if material.representation == "original":
                record["original"] = record["content"]
                declared_hash = material.metadata.get("sha256")
                if declared_hash is not None:
                    if not isinstance(declared_hash, str) or not re.fullmatch(
                        r"[a-fA-F0-9]{64}", declared_hash
                    ):
                        raise MaterialError("invalid_sha256", "Declared SHA-256 is not valid")
                    if declared_hash.lower() != record["content"]["sha256"]:
                        raise MaterialError(
                            "hash_mismatch", "Declared SHA-256 differs from acquired content"
                        )
            if (
                material.representation == "original"
                and material.declared_size is not None
                and material.declared_size != len(raw)
            ):
                raise MaterialError(
                    "size_mismatch",
                    "Declared size differs from acquired content",
                    declared=material.declared_size,
                    actual=len(raw),
                )
            if material.issues:
                continue
            if not material.selected:
                record["state"] = "preserved"
                continue
            if material.representation == "provided_text":
                inspection = inspect_material(raw, "provided.txt", "text/plain", limits)
                record["issues"].append(
                    asdict(
                        Issue(
                            "original_not_provided",
                            "Provided text does not establish original-file coverage",
                        )
                    )
                )
            else:
                inspection = inspect_material(
                    raw,
                    material.filename,
                    material.mime,
                    limits,
                    allow_markdown=(
                        material.kind == "body"
                        and material.representation == "derived_text"
                        and material.metadata.get("markdown_conversion", {}).get("state")
                        == "converted"
                    ),
                )
            record["format"] = inspection.format
            record["scope"] = inspection.scope
            record["issues"].extend(asdict(issue) for issue in inspection.issues)
            if inspection.route is None:
                continue
            task_spec = {
                "content_sha256": record["content"]["sha256"],
                "format": inspection.format,
                "route": inspection.route,
                "parser": asdict(parser_config),
            }
            task_id = _digest(_json_bytes(task_spec))
            if task_id not in tasks:
                tasks[task_id] = {
                    "parse_task_id": task_id,
                    **task_spec,
                    "content": record["content"],
                    "scope": inspection.scope,
                    "material_ids": [],
                    "execution_state": "not_started",
                }
            tasks[task_id]["material_ids"].append(material.material_id)
            record["parse_task_id"] = task_id
            record["state"] = "ready"
        except MaterialError as error:
            record["issues"].append(asdict(error.issue))

    ready = sum(record["state"] == "ready" for record in records)
    blocked = sum(record["state"] == "blocked" for record in records)
    state = "ready" if ready and not blocked else "partial" if ready else "blocked"
    issues = [asdict(issue) for issue in bundle.issues]
    if not ready and not blocked:
        issues.append(
            asdict(Issue("empty_content", "No nonempty material was selected for parsing"))
        )
    result = {
        "schema_version": "doccase-preprocessing/0.1",
        "task_id": bundle.task_id,
        "state": state,
        "emails": bundle.emails,
        "metadata": bundle.metadata,
        "issues": issues,
        "materials": records,
        "parse_tasks": list(tasks.values()),
        "summary": {
            "registered": len(records),
            "ready": ready,
            "blocked": blocked,
            "preserved": len(records) - ready - blocked,
            "unique_parse_tasks": len(tasks),
        },
        "limits": asdict(limits),
        "context_requirements": {
            "identity": ["email_role", "sender", "signature", "source_material_id"],
            "transport": ["location_roles", "alternatives", "time_qualifiers", "trade_terms"],
            "cargo": ["description", "negation", "constraint_scope"],
            "quantity_dimensions": ["row_identity", "headers", "units", "merged_cells"],
            "weight_volume": ["gross_net", "each_per_total", "original_units", "totals"],
            "grouping": ["file_sheet_boundaries", "package_hierarchy", "correction_target"],
        },
        "parsing_completed": False,
        "business_fields_verified": False,
    }
    # A new run never overwrites prior evidence or claims business task idempotency.
    relative = Path("manifests") / f"{uuid4().hex}.json"
    result["manifest_path"] = relative.as_posix()
    with (root / relative).open("xb") as stream:
        stream.write(_json_bytes(result) + b"\n")
    return result
