"""Wire a local intake to the existing manifest and a simulated receiving reader."""

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

from .inputs import _stable_id, adapt_eml, adapt_request
from .markdown_body import convert_body, validate_mapping
from .models import Bundle, InputError, Limits, Material
from .pipeline import _store_content, prepare


def _request_references(payload):
    references = {}

    def add_email(email, path):
        if not isinstance(email, dict) or not isinstance(email.get("attachments", []), list):
            return
        for index, attachment in enumerate(email.get("attachments", [])):
            if isinstance(attachment, dict):
                references[f"{path}.attachments[{index}]"] = attachment.get("oss_url")

    if not isinstance(payload, dict):
        return references
    current = payload.get("email")
    add_email(current, "email")
    context = payload.get("context")
    groups = [
        (context, "ancestors", "context.ancestors"),
        (context, "supplementary", "context.supplementary"),
        (context, "emails", "context.emails"),
        (payload, "history_emails", "history_emails"),
        (current, "history_emails", "email.history_emails"),
    ]
    for owner, key, path in groups:
        if isinstance(owner, dict) and isinstance(owner.get(key, []), list):
            for index, email in enumerate(owner.get(key, [])):
                add_email(email, f"{path}[{index}]")
    return references


def _source_position(material):
    return {
        "material_id": material.material_id,
        "source_path": material.metadata.get("source_path"),
        "mime_path": material.metadata.get("mime_path"),
        "parent_material_id": material.parent_material_id,
    }


def _record_position(record):
    return {
        "material_id": record["material_id"],
        "source_path": record["metadata"].get("source_path"),
        "mime_path": record["metadata"].get("mime_path"),
        "parent_material_id": record["parent_material_id"],
    }


def _protect_manifest_references(bundle, references):
    protected = sorted(
        {
            reference
            for reference in references
            if isinstance(reference, str) and reference.lower().startswith(("https://", "http://"))
        },
        key=len,
        reverse=True,
    )
    if not protected:
        return

    def contains_reference(value):
        return isinstance(value, str) and any(reference in value for reference in protected)

    def redact(value):
        if isinstance(value, str):
            for reference in protected:
                value = value.replace(reference, "[controlled reference withheld]")
        elif isinstance(value, list):
            value = [redact(item) for item in value]
        elif isinstance(value, dict):
            if any(contains_reference(key) for key in value):
                raise InputError("A controlled reference cannot be used as a manifest key")
            value = {key: redact(item) for key, item in value.items()}
        return value

    def redact_issues(issues):
        result = []
        for issue in issues:
            if contains_reference(issue.code):
                raise InputError("A controlled reference cannot be used as an issue code")
            result.append(
                replace(issue, message=redact(issue.message), details=redact(issue.details))
            )
        return result

    if contains_reference(bundle.task_id):
        raise InputError("A controlled reference cannot be used as a task identifier")
    for material in bundle.materials:
        identifiers = (
            material.material_id,
            material.email_ref_id,
            material.parent_material_id,
            material.alternative_group,
            material.filename,
            material.mime,
            material.cid,
        )
        if any(contains_reference(identifier) for identifier in identifiers):
            raise InputError(
                "A controlled reference cannot be used as a material identifier or label"
            )
        material.metadata = redact(material.metadata)
        material.issues = redact_issues(material.issues)
    bundle.emails = redact(bundle.emails)
    bundle.metadata = redact(bundle.metadata)
    bundle.issues = redact_issues(bundle.issues)


def run_local_handoff(
    raw,
    output_dir,
    *,
    input_format,
    request_reference,
    task_id=None,
    email_ref_id=None,
    limits=None,
    fetch_attachment=None,
    body_input="original",
):
    """Capture intake data, prepare a manifest, then receive it without running a parser."""
    limits = limits if limits is not None else Limits()
    if body_input not in {"original", "markdown"}:
        raise ValueError("Unsupported local body input")
    if len(raw) > limits.max_input_bytes:
        raise InputError(f"Input exceeds {limits.max_input_bytes} bytes")
    fingerprint = hashlib.sha256(raw).hexdigest()
    references = {}
    upstream_issues = []
    if input_format == "json":
        payload = json.loads(raw)
        references = _request_references(payload)
        if isinstance(payload, dict) and isinstance(payload.get("context"), dict):
            upstream_issues = copy.deepcopy(payload["context"].get("issues", []))
        bundle = adapt_request(payload, limits)
    elif input_format == "eml":
        bundle = adapt_eml(raw, task_id=task_id, email_ref_id=email_ref_id, limits=limits)
        if body_input == "markdown":
            _derive_markdown_body(bundle, output_dir, limits)
    else:
        raise InputError("Input format must be json or eml")

    return _prepare_intake(
        bundle,
        output_dir,
        fingerprint,
        request_reference,
        references,
        upstream_issues,
        limits,
        fetch_attachment,
    )


def _derive_markdown_body(bundle, output_dir, limits):
    bodies = [
        m
        for m in bundle.materials
        if m.kind == "body"
        and m.role == "current"
        and m.selected
        and m.content is not None
        and not m.issues
    ]
    if len(bodies) != 1:
        return
    source = bodies[0]
    markdown, mapping, reason = convert_body(source.content, source.mime)
    material_bytes = sum(
        len(m.content)
        for m in bundle.materials
        if m.kind != "original_email" and m.content is not None
    )
    if reason is None and (
        len(markdown) > limits.max_material_bytes
        or material_bytes + len(markdown) > limits.max_input_bytes
        or len(bundle.materials) + 1 > limits.max_materials
    ):
        reason = "derived_material_limit"
    if reason is not None:
        source.metadata["markdown_conversion"] = {"state": "fallback", "reason": reason}
        return
    mapping_raw = json.dumps(mapping, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if (
        len(mapping_raw) > limits.max_material_bytes
        or material_bytes + len(markdown) + len(mapping_raw) > limits.max_input_bytes
    ):
        source.metadata["markdown_conversion"] = {"state": "fallback", "reason": "source_map_limit"}
        return
    root = Path(output_dir)
    (root / "objects").mkdir(parents=True, exist_ok=True)
    mapping_ref = _store_content(root, mapping_raw)
    conversion = {
        "state": "converted",
        "version": mapping["version"],
        "source_sha256": mapping["source_sha256"],
        "markdown_sha256": mapping["markdown_sha256"],
        "mapping": mapping_ref,
    }
    source.selected = False
    bundle.materials.append(
        Material(
            _stable_id(bundle.task_id, source.material_id + ".markdown"),
            source.email_ref_id,
            source.role,
            "body",
            "body.md",
            "text/markdown",
            content=markdown,
            parent_material_id=source.material_id,
            representation="derived_text",
            metadata={
                "mime_path": source.metadata.get("mime_path"),
                "markdown_conversion": conversion,
            },
        )
    )


def run_local_file_handoff(raw, output_dir, *, filename, mime, task_id, limits=None):
    """Prepare one independent file, retaining its actual bytes and upload identity."""
    limits = limits if limits is not None else Limits()
    if len(raw) > limits.max_input_bytes:
        raise InputError(f"Input exceeds {limits.max_input_bytes} bytes")
    material = Material(
        "file",
        task_id,
        "current",
        "attachment",
        filename,
        mime,
        content=raw,
        metadata={"source_path": "file"},
    )
    # The local channel uses email_ref_id as its material scope key. An independent
    # file gets its own scope without any fabricated sender, subject or MIME origin.
    scope = {"email_ref_id": task_id, "role": "current", "source": {"artifact_kind": "local_file"}}
    bundle = Bundle(task_id, [scope], [material], metadata={"input_format": "file"})
    return _prepare_intake(
        bundle,
        output_dir,
        hashlib.sha256(raw).hexdigest(),
        "local-file:" + task_id,
        {},
        [],
        limits,
        None,
    )


def _prepare_intake(
    bundle,
    output_dir,
    fingerprint,
    request_reference,
    references,
    upstream_issues,
    limits,
    fetch_attachment,
):
    _protect_manifest_references(bundle, [request_reference, *references.values()])
    materials = {}
    for material in bundle.materials:
        position = _source_position(material)
        reference = references.get(position["source_path"])
        if reference != material.locator:
            reference = None
        materials[material.material_id] = {**position, "original_reference": reference}
    intake_context = {
        "task_id": bundle.task_id,
        "request_fingerprint": fingerprint,
        "request_reference": request_reference,
        "upstream_issues": upstream_issues,
        "materials": materials,
    }
    prepared = prepare(bundle, output_dir, limits=limits, fetch_attachment=fetch_attachment)
    manifest_path = Path(output_dir) / prepared["manifest_path"]
    intake_context["manifest_sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    received = receive_handoff(
        manifest_path,
        output_dir,
        expected_task_id=bundle.task_id,
        expected_request_fingerprint=fingerprint,
        intake_context=intake_context,
    )
    return {
        "manifest_path": manifest_path,
        "request_fingerprint": fingerprint,
        "intake_context": intake_context,
        "received": received,
    }


def _read_content(root, descriptor):
    relative = Path(descriptor["path"])
    if relative.is_absolute():
        raise ValueError("Content path must be relative to the trusted root")
    target = (root / relative).resolve(strict=True)
    if not target.is_relative_to(root):
        raise ValueError("Content path escapes the trusted root")
    raw = target.read_bytes()
    if hashlib.sha256(raw).hexdigest() != descriptor["sha256"]:
        raise ValueError("Content SHA-256 mismatch")
    if len(raw) != descriptor["size_bytes"]:
        raise ValueError("Content size mismatch")
    return raw


def _material_issues(issues, position):
    result = []
    for issue in issues:
        details = issue.get("details")
        details = details if isinstance(details, dict) else {}
        identity = [
            (key, source[key])
            for source in (issue, details)
            for key in ("material_id", "source_path", "mime_path")
            if key in source
        ]
        if identity and all(position[key] == value for key, value in identity):
            result.append(copy.deepcopy(issue))
    return result


def receive_handoff(
    manifest_path,
    content_root,
    *,
    expected_task_id,
    expected_request_fingerprint,
    intake_context,
):
    """Read and verify the on-disk 0.1 manifest and its actual material bytes."""
    root = Path(content_root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Trusted content root is not a directory")
    manifest_bytes = Path(manifest_path).read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest["schema_version"] != "doccase-preprocessing/0.1":
        raise ValueError("Unsupported manifest version")
    if manifest["task_id"] != expected_task_id:
        raise ValueError("Manifest task_id does not match the expected task")

    materials = {record["material_id"]: record for record in manifest["materials"]}
    tasks = {task["parse_task_id"]: task for task in manifest["parse_tasks"]}
    if len(materials) != len(manifest["materials"]) or len(tasks) != len(manifest["parse_tasks"]):
        raise ValueError("Duplicate material or parse task identifier")
    for record in materials.values():
        parent = record["parent_material_id"]
        if parent is not None and parent not in materials:
            raise ValueError("Material parent reference is missing")
        task_id = record["parse_task_id"]
        if task_id is not None and (
            task_id not in tasks or record["material_id"] not in tasks[task_id]["material_ids"]
        ):
            raise ValueError("Material-to-task reference is broken")
        if record["state"] == "ready" and task_id is None:
            raise ValueError("Ready material has no parse task")
        conversion = record["metadata"].get("markdown_conversion")
        if conversion and conversion.get("state") == "converted":
            parent_record = materials.get(parent)
            if (
                record["kind"] != "body"
                or record["format"] != "markdown"
                or not parent_record
                or parent_record["kind"] != "body"
                or parent_record["state"] != "preserved"
            ):
                raise ValueError("Markdown body parent does not match its preserved source")
            mapping = json.loads(_read_content(root, conversion["mapping"]))
            validate_mapping(
                _read_content(root, parent_record["content"]),
                _read_content(root, record["content"]),
                mapping,
            )
            if (
                conversion["source_sha256"] != mapping["source_sha256"]
                or conversion["markdown_sha256"] != mapping["markdown_sha256"]
                or conversion["version"] != mapping["version"]
            ):
                raise ValueError("Markdown conversion metadata mismatch")
    for task_id, task in tasks.items():
        material_ids = task["material_ids"]
        if not material_ids or len(material_ids) != len(set(material_ids)):
            raise ValueError("Parse task has invalid material references")
        for material_id in material_ids:
            if material_id not in materials or materials[material_id]["parse_task_id"] != task_id:
                raise ValueError("Task-to-material reference is broken")
            content = materials[material_id]["content"]
            if (
                content is None
                or content != task["content"]
                or content["sha256"] != task["content_sha256"]
            ):
                raise ValueError("Parse task content does not match its material")

    bound = bool(
        intake_context
        and intake_context.get("task_id") == expected_task_id
        and intake_context.get("request_fingerprint") == expected_request_fingerprint
        and intake_context.get("request_reference")
        and intake_context.get("manifest_sha256") == hashlib.sha256(manifest_bytes).hexdigest()
    )
    issues = intake_context.get("upstream_issues", []) if bound else []
    received_materials = []
    for record in manifest["materials"]:
        content = _read_content(root, record["content"]) if record["content"] else None
        original = record["original"]
        if original:
            if record["representation"] != "original":
                raise ValueError("A text derivative cannot be its original file")
            if original != record["content"]:
                _read_content(root, original)
        position = _record_position(record)
        sources = intake_context.get("materials", {}) if bound else {}
        source = sources.get(record["material_id"])
        matched = source is not None and all(
            source.get(key) == value for key, value in position.items()
        )
        reference = source.get("original_reference") if matched else None
        received_materials.append(
            {
                "record": record,
                "content": content,
                "source_bound": matched,
                "original_reference": reference,
                "original_reference_available": bool(reference),
                "original_content_integrity_checked": original is not None,
                "upstream_issues": _material_issues(issues, position) if matched else [],
            }
        )
    return {
        "manifest": manifest,
        "materials": received_materials,
        "intake_bound": bound,
        "request_reference": intake_context.get("request_reference") if bound else None,
        "upstream_issues": copy.deepcopy(issues),
    }
